package es.radarsetas

import android.app.Application
import android.content.Context
import androidx.work.Worker
import androidx.work.WorkerParameters
import com.chaquo.python.Kwarg

/** Actualización en segundo plano: la misma que al abrir la app, esperando a que termine. */
class RefreshWorker(context: Context, params: WorkerParameters) : Worker(context, params) {
    override fun doWork(): Result {
        RadarApplication.mobile(applicationContext as Application)
            .callAttr("refresh", Kwarg("wait", true), Kwarg("if_older", true))
        return Result.success()
    }
}
