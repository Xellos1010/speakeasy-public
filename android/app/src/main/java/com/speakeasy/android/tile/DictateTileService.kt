package com.speakeasy.android.tile

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import android.widget.RemoteViews
import com.speakeasy.android.R
import com.speakeasy.android.ui.MainActivity

internal fun dictateIntent(ctx: Context, source: String) = Intent(ctx, MainActivity::class.java)
    .setAction(MainActivity.ACTION_DICTATE)
    .putExtra("source", source)
    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)

/** Quick Settings tile: opens SpeakEasy already listening. */
class DictateTileService : TileService() {
    override fun onStartListening() {
        qsTile?.apply { state = Tile.STATE_INACTIVE; label = "Dictate"; updateTile() }
    }

    @SuppressLint("StartActivityAndCollapseDeprecated")
    override fun onClick() {
        val i = dictateIntent(this, "tile")
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 1, i,
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
        } else {
            @Suppress("DEPRECATION") startActivityAndCollapse(i)
        }
    }
}

/** Home-screen widget: one big mic button that opens SpeakEasy listening. */
class DictateWidget : AppWidgetProvider() {
    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) {
        val pi = PendingIntent.getActivity(ctx, 2, dictateIntent(ctx, "widget"),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val rv = RemoteViews(ctx.packageName, R.layout.widget_dictate).apply {
            setOnClickPendingIntent(R.id.widget_mic, pi)
        }
        ids.forEach { mgr.updateAppWidget(it, rv) }
    }

    companion object {
        fun refresh(ctx: Context) {
            val mgr = AppWidgetManager.getInstance(ctx)
            val ids = mgr.getAppWidgetIds(ComponentName(ctx, DictateWidget::class.java))
            if (ids.isNotEmpty()) DictateWidget().onUpdate(ctx, mgr, ids)
        }
    }
}
