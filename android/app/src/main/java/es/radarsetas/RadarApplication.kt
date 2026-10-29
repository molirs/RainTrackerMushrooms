package es.radarsetas

import android.app.Application
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.util.concurrent.TimeUnit

/** Arranca Python (app/mobile.py) una vez por proceso y programa la actualización periódica. */
class RadarApplication : Application() {

    override fun onCreate() {
        super.onCreate()
        // Cada pocas horas aunque no se abra la app: AEMET solo da las últimas 24 h de datos horarios
        val work = PeriodicWorkRequestBuilder<RefreshWorker>(3, TimeUnit.HOURS)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .build()
        WorkManager.getInstance(this)
            .enqueueUniquePeriodicWork("refresh", ExistingPeriodicWorkPolicy.KEEP, work)
    }

    companion object {
        @Volatile private var module: PyObject? = null

        /** Módulo app.mobile ya inicializado. Arrancar Python tarda ~1 s: no llamar desde el hilo principal. */
        fun mobile(app: Application): PyObject = module ?: synchronized(this) {
            module ?: run {
                if (!Python.isStarted()) Python.start(AndroidPlatform(app))
                Python.getInstance().getModule("app.mobile").also {
                    it.callAttr("init", app.filesDir.absolutePath, BuildConfig.VERSION_NAME)
                    module = it
                }
            }
        }
    }
}
