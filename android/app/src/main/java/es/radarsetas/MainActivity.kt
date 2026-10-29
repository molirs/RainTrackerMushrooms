package es.radarsetas

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.res.Configuration
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.window.OnBackInvokedDispatcher
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.chaquo.python.Kwarg
import java.io.ByteArrayInputStream
import java.io.FileNotFoundException
import kotlin.concurrent.thread

/**
 * El mismo frontend que la web (app/static, empaquetado en assets) en un WebView. Las peticiones a
 * /api/... no salen del móvil: las contesta app/mobile.py, que es quien descarga los datos de las fuentes.
 */
class MainActivity : Activity() {

    private lateinit var webView: WebView
    private lateinit var updates: UpdateChecker

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        updates = UpdateChecker(this)

        WindowCompat.setDecorFitsSystemWindows(window, false)
        webView = WebView(this)
        // El WebView no respeta su propio padding: se deja el hueco de las barras del sistema en el contenedor
        val root = FrameLayout(this).apply { addView(webView) }
        setContentView(root)
        val night = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK == Configuration.UI_MODE_NIGHT_YES
        WindowInsetsControllerCompat(window, webView).apply {
            isAppearanceLightStatusBars = !night
            isAppearanceLightNavigationBars = !night
        }
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
        }
        webView.addJavascriptInterface(Bridge(), "RadarApp")
        webView.webViewClient = Client()
        webView.loadUrl(ORIGIN + "/")

        if (Build.VERSION.SDK_INT >= 33) {
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT) { onBack() }
        }
        updates.check(manual = false)
    }

    override fun onResume() {
        super.onResume()
        updates.onResume()
        // Al abrir o volver a la app: actualizar si hace rato de la última vez y avisar al mapa
        thread(name = "refresh-on-resume") {
            RadarApplication.mobile(application).callAttr("refresh", Kwarg("if_older", true))
            runOnUiThread { webView.evaluateJavascript("window.radarResume && window.radarResume()", null) }
        }
    }

    @Deprecated("Solo Android < 13; desde 13 va por OnBackInvokedDispatcher")
    override fun onBackPressed() = onBack()

    private fun onBack() {
        // Primero se cierra la ficha o el panel de filtros; si no había nada abierto, se sale
        webView.evaluateJavascript("window.radarBack ? window.radarBack() : false") { handled ->
            if (handled != "true") finish()
        }
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }

    private inner class Bridge {
        @JavascriptInterface
        fun checkForUpdates() = runOnUiThread { updates.check(manual = true) }
    }

    private inner class Client : WebViewClient() {
        override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
            val url = request.url
            if (url.host != HOST) return null  // teselas de OpenStreetMap, etc.: van a internet
            val path = url.path ?: "/"
            return try {
                if (path.startsWith("/api/")) api(request.method, path, url.encodedQuery ?: "") else asset(path)
            } catch (e: Exception) {
                Log.e(TAG, "Error en $path", e)
                WebResourceResponse("text/plain", "utf-8", 500, "Error", null, ByteArrayInputStream(e.toString().toByteArray()))
            }
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            if (request.url.host == HOST) return false
            startActivity(Intent(Intent.ACTION_VIEW, request.url))  // enlaces externos, en el navegador
            return true
        }
    }

    private fun api(method: String, path: String, query: String): WebResourceResponse {
        val out = RadarApplication.mobile(application).callAttr("handle", method, path, query).asList()
        val status = out[0].toInt()
        val body = out[2].toString().toByteArray(Charsets.UTF_8)
        return WebResourceResponse(out[1].toString(), "utf-8", status, if (status < 400) "OK" else "Error",
            mapOf("Cache-Control" to "no-store"), ByteArrayInputStream(body))
    }

    private fun asset(path: String): WebResourceResponse {
        val name = when {
            path == "/" -> "index.html"
            path.startsWith("/static/") -> path.removePrefix("/static/")
            else -> path.removePrefix("/")
        }
        return try {
            WebResourceResponse(mimeOf(name), "utf-8", assets.open(name))
        } catch (e: FileNotFoundException) {
            WebResourceResponse("text/plain", "utf-8", 404, "Not Found", null, ByteArrayInputStream(ByteArray(0)))
        }
    }

    private fun mimeOf(name: String) = when (name.substringAfterLast('.').lowercase()) {
        "html" -> "text/html"
        "js" -> "text/javascript"
        "css" -> "text/css"
        "json" -> "application/json"
        "png" -> "image/png"
        "svg" -> "image/svg+xml"
        "ico" -> "image/x-icon"
        else -> "application/octet-stream"
    }

    companion object {
        private const val TAG = "RadarSetas"
        private const val HOST = "appassets.androidplatform.net"  // dominio reservado para contenido local
        private const val ORIGIN = "https://$HOST"
    }
}
