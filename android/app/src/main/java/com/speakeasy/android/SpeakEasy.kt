package com.speakeasy.android

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.speakeasy.android.audio.Recorder
import com.speakeasy.android.data.HistoryEntry
import com.speakeasy.android.data.HistoryStore
import com.speakeasy.android.data.SecureStore
import com.speakeasy.android.data.Settings
import com.speakeasy.android.net.ApiClient
import com.speakeasy.android.net.ApiException
import com.speakeasy.android.net.Transcript
import com.speakeasy.android.queue.DictationQueue
import com.speakeasy.android.queue.SendOutcome
import com.speakeasy.android.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.UUID
import java.util.concurrent.TimeUnit

sealed class Conn(val label: String) {
    object Unknown : Conn("Checking…")
    object NotPaired : Conn("Not paired")
    data class Connected(val ms: Long) : Conn("Connected")
    data class Offline(val reason: String) : Conn("Offline")
    data class Error(val reason: String) : Conn("Error")
}

enum class Phase { Idle, Recording, Sending }

/**
 * Process-wide dictation controller shared by the app UI, the keyboard (IME), the
 * Quick Settings tile and the widget. Every recording is written to the on-disk queue
 * BEFORE upload and deleted only after the server confirms a transcript.
 */
class SpeakEasy private constructor(private val ctx: Context) {
    val settings = Settings(ctx)
    val secure = SecureStore(ctx)
    val queue = DictationQueue(File(ctx.filesDir, "queue"))
    val historyStore = HistoryStore(File(ctx.filesDir, "history.json"))
    private val recorder = Recorder()
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val eventBuf = mutableListOf<JSONObject>()

    private val _conn = MutableStateFlow<Conn>(if (secure.paired) Conn.Unknown else Conn.NotPaired)
    val conn: StateFlow<Conn> = _conn
    private val _phase = MutableStateFlow(Phase.Idle)
    val phase: StateFlow<Phase> = _phase
    val level: StateFlow<Float> get() = recorder.level
    private val _history = MutableStateFlow(historyStore.all())
    val history: StateFlow<List<HistoryEntry>> = _history
    private val _queued = MutableStateFlow(queue.size())
    val queued: StateFlow<Int> = _queued
    private val _current = MutableStateFlow<HistoryEntry?>(_history.value.firstOrNull())
    val current: StateFlow<HistoryEntry?> = _current
    private val _notice = MutableStateFlow<String?>(null)
    val notice: StateFlow<String?> = _notice

    fun api() = ApiClient(settings.serverUrl, { secure.token })

    fun hasMic() = ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) ==
        PackageManager.PERMISSION_GRANTED

    fun setNotice(s: String?) { _notice.value = s }

    fun select(e: HistoryEntry) { _current.value = e }

    // ---------------------------------------------------------------- recording

    /** Returns null on success, else a human-readable reason. */
    fun startRecording(): String? {
        if (!secure.paired) return "Pair with your Mac first"
        if (!hasMic()) return "Microphone permission needed — open SpeakEasy"
        if (_phase.value != Phase.Idle) return null
        return try {
            recorder.start(); _phase.value = Phase.Recording; _notice.value = null; null
        } catch (e: Exception) { e.message ?: "Microphone unavailable" }
    }

    fun cancelRecording() { recorder.cancel(); _phase.value = Phase.Idle }

    /**
     * Stops recording, persists to the queue, then tries an immediate upload.
     * [onDone] runs on the IO thread with the final history entry (done / queued / error).
     */
    fun stopAndSend(mode: String, appHint: String? = null, source: String = "app",
                    onDone: (HistoryEntry) -> Unit = {}) {
        if (_phase.value != Phase.Recording) return
        val wav = recorder.stop()
        _phase.value = Phase.Sending
        scope.launch {
            val item = queue.enqueue(wav, mode, appHint)   // never lose a dictation
            _queued.value = queue.size()
            logEvent("app_usage", "mode" to mode, "source" to source, "app_hint" to appHint,
                "audio_ms" to (wav.size - 44) * 1000L / 32000L)
            val entry = try {
                val t = api().transcribe(queue.audio(item.id), settings.deviceId, mode, appHint, item.id)
                queue.remove(item.id)
                _conn.value = Conn.Connected(t.roundTripMs)
                done(item.id, t, item.createdAt)
            } catch (e: Exception) {
                val reason = reasonOf(e)
                queue.markFailed(item.id, reason)
                val transient = e !is ApiException || e.transient
                if (transient) _conn.value = Conn.Offline(reason) else _conn.value = Conn.Error(reason)
                scheduleDrain(ctx)
                HistoryEntry(item.id, null, "", "", mode, item.createdAt,
                    if (transient) "queued" else "error", error = reason)
            }
            historyStore.upsert(entry)
            refresh(); _current.value = entry
            _phase.value = Phase.Idle
            flushEvents()
            onDone(entry)
        }
    }

    private fun done(id: String, t: Transcript, createdAt: Long) = HistoryEntry(
        id, t.utteranceId.ifEmpty { null }, t.text, t.rawText, t.mode, createdAt, "done",
        rewritten = t.rewritten, latencyMs = t.roundTripMs)

    private fun refresh() { _history.value = historyStore.all(); _queued.value = queue.size() }

    // ---------------------------------------------------------------- queue

    /** Sends everything queued. Returns remaining count. Safe to call from a Worker. */
    fun drainQueue(): Int {
        if (!secure.paired) return queue.size()
        val api = api()
        val delivered = mutableListOf<HistoryEntry>()
        queue.drain(onDelivered = { item, payload ->
            val e = done(item.id, payload as Transcript, item.createdAt)
            historyStore.upsert(e); delivered += e
        }) { item, file ->
            try {
                val t = api.transcribe(file, settings.deviceId, item.mode, item.appHint, item.id)
                SendOutcome.Delivered(t)
            } catch (e: ApiException) {
                if (e.transient) SendOutcome.Retry(reasonOf(e)) else SendOutcome.Rejected(reasonOf(e))
            } catch (e: IOException) { SendOutcome.Retry(reasonOf(e)) }
        }
        refresh()
        if (delivered.isNotEmpty()) {
            _conn.value = Conn.Connected(0)
            _current.value = delivered.last()
            notifyDelivered(delivered)
        }
        return queue.size()
    }

    // ---------------------------------------------------------------- server

    fun checkHealth(onResult: (Conn) -> Unit = {}) {
        scope.launch {
            val c = if (!secure.paired) Conn.NotPaired else try {
                val h = api().health(); if (h.ok) Conn.Connected(h.latencyMs) else Conn.Error(h.detail.ifEmpty { "server not ready" })
            } catch (e: Exception) { Conn.Offline(reasonOf(e)) }
            _conn.value = c
            if (c is Conn.Connected && queue.size() > 0) scheduleDrain(ctx)
            onResult(c)
        }
    }

    /** Probe a URL before pairing (no token). */
    suspend fun probe(url: String): Conn = withContext(Dispatchers.IO) {
        try {
            val h = ApiClient(url, { null }).health()
            if (h.ok) Conn.Connected(h.latencyMs) else Conn.Error(h.detail)
        } catch (e: ApiException) {
            if (e.code == 401 || e.code == 403) Conn.Connected(0) else Conn.Error(reasonOf(e))
        } catch (e: Exception) { Conn.Offline(reasonOf(e)) }
    }

    suspend fun pair(url: String, code: String, deviceName: String): String? = withContext(Dispatchers.IO) {
        try {
            val r = ApiClient(url, { null }).pair(code, deviceName)
            settings.serverUrl = url; settings.deviceName = deviceName
            r.deviceId?.let { settings.deviceId = it }
            secure.token = r.token
            _conn.value = Conn.Unknown
            logEvent("paired", "device_name" to deviceName)
            checkHealth(); flushEvents()
            null
        } catch (e: Exception) { reasonOf(e) }
    }

    fun unpair() { secure.token = null; _conn.value = Conn.NotPaired }

    fun sendFeedback(e: HistoryEntry, corrected: String, onResult: (String?) -> Unit = {}) {
        historyStore.update(e.id) { it.copy(text = corrected, edited = true) }
        refresh(); _current.value = _history.value.firstOrNull { it.id == e.id }
        scope.launch {
            val err = try {
                if (e.utteranceId != null) api().feedback(e.utteranceId, corrected, settings.deviceId); null
            } catch (ex: Exception) { reasonOf(ex) }
            logEvent("edit", "utterance_id" to e.utteranceId, "mode" to e.mode,
                "chars_before" to e.text.length, "chars_after" to corrected.length)
            flushEvents()
            onResult(err)
        }
    }

    // ---------------------------------------------------------------- events

    /** Contract v1 event: {type, ts (ms), id, data:{...}}. */
    fun logEvent(type: String, vararg fields: Pair<String, Any?>) {
        val data = JSONObject()
        for ((k, v) in fields) if (v != null) data.put(k, v)
        val o = JSONObject().put("type", type).put("ts", System.currentTimeMillis())
            .put("id", "and_" + newId()).put("data", data)
        synchronized(eventBuf) { eventBuf += o; if (eventBuf.size > 500) eventBuf.removeAt(0) }
    }

    fun flushEvents() {
        val batch = synchronized(eventBuf) { eventBuf.toList().also { eventBuf.clear() } }
        if (batch.isEmpty() || !secure.paired) { synchronized(eventBuf) { eventBuf.addAll(0, batch) }; return }
        scope.launch {
            try { api().events(settings.deviceId, batch) } catch (_: Exception) {
                synchronized(eventBuf) { eventBuf.addAll(0, batch) }   // retry with the next flush
            }
        }
    }

    // ---------------------------------------------------------------- notify

    @SuppressLint("MissingPermission")
    private fun notifyDelivered(list: List<HistoryEntry>) {
        val nm = NotificationManagerCompat.from(ctx)
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(ctx,
                Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
        val pi = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(if (list.size == 1) "Queued dictation transcribed" else "${list.size} queued dictations transcribed")
            .setContentText(list.last().text.take(120))
            .setStyle(NotificationCompat.BigTextStyle().bigText(list.last().text))
            .setContentIntent(pi).setAutoCancel(true).build()
        nm.notify(1001, n)
    }

    companion object {
        const val CHANNEL = "speakeasy_queue"
        val MODES = listOf("raw", "clean", "email", "prompt")

        @Volatile private var inst: SpeakEasy? = null
        fun get(ctx: Context): SpeakEasy = inst ?: synchronized(this) {
            inst ?: SpeakEasy(ctx.applicationContext).also { inst = it }
        }

        fun reasonOf(e: Throwable): String = when (e) {
            is ApiException -> if (e.code == 401) "Not authorised — re-pair this phone" else e.message ?: "HTTP ${e.code}"
            is java.net.UnknownHostException -> "Server not found — is Tailscale on?"
            is java.net.ConnectException -> "Server unreachable — is the Mac awake?"
            is java.net.SocketTimeoutException -> "Server timed out"
            is javax.net.ssl.SSLException -> "TLS error: ${e.message?.take(80)}"
            else -> e.message?.take(120) ?: e.javaClass.simpleName
        }

        fun createChannel(ctx: Context) {
            if (Build.VERSION.SDK_INT >= 26) {
                ctx.getSystemService(NotificationManager::class.java).createNotificationChannel(
                    NotificationChannel(CHANNEL, "Queued dictations", NotificationManager.IMPORTANCE_DEFAULT))
            }
        }

        fun scheduleDrain(ctx: Context) {
            val req = OneTimeWorkRequestBuilder<QueueWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(ctx).enqueueUniqueWork("speakeasy-drain", ExistingWorkPolicy.REPLACE, req)
        }

        fun newId(): String = UUID.randomUUID().toString()
    }
}

/** Retries queued dictations whenever a network is up, with exponential backoff. */
class QueueWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val remaining = SpeakEasy.get(applicationContext).drainQueue()
        return if (remaining == 0) Result.success() else Result.retry()
    }
}
