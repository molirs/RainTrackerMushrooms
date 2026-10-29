package es.radarsetas

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import android.util.Log
import android.widget.ProgressBar
import android.widget.Toast
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * Al abrir la app pregunta a GitHub por la última versión publicada (Releases del repositorio
 * BuildConfig.GITHUB_REPO). Si es más nueva que la instalada ofrece descargarla e instalarla.
 */
class UpdateChecker(private val activity: Activity) {

    private data class Release(val version: String, val notes: String, val apkUrl: String)

    private var pending: Release? = null  // esperando a que el usuario permita instalar apps

    fun check(manual: Boolean) = thread(name = "update-check") {
        val result = runCatching { latestRelease() }
        activity.runOnUiThread {
            if (activity.isFinishing) return@runOnUiThread
            val release = result.getOrNull()
            when {
                release != null && isNewer(release.version, BuildConfig.VERSION_NAME) -> offer(release)
                !manual -> Unit  // en el chequeo automático no se molesta si no hay nada (o no hay red)
                result.isFailure -> toast("No se ha podido consultar GitHub: ${result.exceptionOrNull()?.message}")
                release == null -> toast("Todavía no hay versiones publicadas")
                else -> toast("Ya tienes la última versión (${BuildConfig.VERSION_NAME})")
            }
        }
    }

    fun onResume() {
        // Vuelta de la pantalla "Instalar apps desconocidas": si ya hay permiso, seguir
        val release = pending ?: return
        if (activity.packageManager.canRequestPackageInstalls()) {
            pending = null
            download(release)
        }
    }

    private fun latestRelease(): Release? {
        val conn = URL("https://api.github.com/repos/${BuildConfig.GITHUB_REPO}/releases/latest")
            .openConnection() as HttpURLConnection
        conn.setRequestProperty("Accept", "application/vnd.github+json")
        conn.setRequestProperty("User-Agent", "RadarSetas/${BuildConfig.VERSION_NAME}")
        conn.connectTimeout = 10_000
        conn.readTimeout = 10_000
        try {
            if (conn.responseCode == 404) return null
            if (conn.responseCode != 200) throw RuntimeException("HTTP ${conn.responseCode}")
            val json = JSONObject(conn.inputStream.bufferedReader().readText())
            val assets = json.getJSONArray("assets")
            val apk = (0 until assets.length()).map { assets.getJSONObject(it) }
                .firstOrNull { it.getString("name").endsWith(".apk") } ?: return null
            return Release(
                version = json.getString("tag_name").removePrefix("v"),
                notes = json.optString("body").trim(),
                apkUrl = apk.getString("browser_download_url"),
            )
        } finally {
            conn.disconnect()
        }
    }

    private fun offer(release: Release) {
        val notes = release.notes.take(1500).ifEmpty { "Sin notas de la versión." }
        AlertDialog.Builder(activity)
            .setTitle("Nueva versión ${release.version}")
            .setMessage("Tienes la ${BuildConfig.VERSION_NAME}.\n\n$notes")
            .setPositiveButton("Instalar") { _, _ -> install(release) }
            .setNegativeButton("Ahora no", null)
            .show()
    }

    private fun install(release: Release) {
        if (activity.packageManager.canRequestPackageInstalls()) return download(release)
        pending = release
        AlertDialog.Builder(activity)
            .setTitle("Permiso necesario")
            .setMessage("Para instalar la actualización, Android necesita que permitas a Radar setas " +
                "instalar aplicaciones. Activa el permiso y vuelve a la app.")
            .setPositiveButton("Abrir ajustes") { _, _ ->
                activity.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${activity.packageName}")))
            }
            .setNegativeButton("Cancelar") { _, _ -> pending = null }
            .show()
    }

    private fun download(release: Release) {
        val bar = ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            setPadding(48, 24, 48, 0)
        }
        val dialog = AlertDialog.Builder(activity)
            .setTitle("Descargando ${release.version}…")
            .setView(bar)
            .setCancelable(false)
            .show()
        thread(name = "update-download") {
            val file = File(activity.cacheDir, "updates/RadarSetas.apk")
            val result = runCatching {
                file.parentFile!!.mkdirs()
                // GitHub redirige a su CDN; HttpURLConnection sigue la redirección (https -> https)
                val conn = URL(release.apkUrl).openConnection() as HttpURLConnection
                conn.setRequestProperty("User-Agent", "RadarSetas/${BuildConfig.VERSION_NAME}")
                try {
                    if (conn.responseCode != 200) throw RuntimeException("HTTP ${conn.responseCode}")
                    val total = conn.contentLengthLong
                    conn.inputStream.use { input ->
                        file.outputStream().use { out ->
                            val buf = ByteArray(64 * 1024)
                            var done = 0L
                            while (true) {
                                val n = input.read(buf)
                                if (n < 0) break
                                out.write(buf, 0, n)
                                done += n
                                if (total > 0) activity.runOnUiThread { bar.progress = (done * 100 / total).toInt() }
                            }
                        }
                    }
                } finally {
                    conn.disconnect()
                }
            }
            activity.runOnUiThread {
                dialog.dismiss()
                result.onSuccess { launchInstaller(file) }.onFailure {
                    Log.e("RadarSetas", "Descarga de la actualización", it)
                    toast("No se ha podido descargar: ${it.message}")
                }
            }
        }
    }

    private fun launchInstaller(file: File) {
        val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.updates", file)
        activity.startActivity(Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
    }

    private fun toast(msg: String) = Toast.makeText(activity, msg, Toast.LENGTH_LONG).show()

    companion object {
        /** "1.10.0" > "1.9.2"; lo que va tras un guion (p. ej. "-dev") no cuenta. */
        fun isNewer(candidate: String, current: String): Boolean {
            fun parts(v: String) = v.split("-")[0].split(".").map { it.toIntOrNull() ?: 0 }
            val a = parts(candidate)
            val b = parts(current)
            for (i in 0 until maxOf(a.size, b.size)) {
                val x = a.getOrElse(i) { 0 }
                val y = b.getOrElse(i) { 0 }
                if (x != y) return x > y
            }
            return false
        }
    }
}
