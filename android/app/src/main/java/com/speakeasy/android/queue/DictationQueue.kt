package com.speakeasy.android.queue

import org.json.JSONObject
import java.io.File
import java.util.UUID

/** A dictation persisted on disk until the server has transcribed it. */
data class QueueItem(
    val id: String,
    val mode: String,
    val createdAt: Long,
    val appHint: String? = null,
    val attempts: Int = 0,
    val lastError: String? = null,
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("id", id); put("mode", mode); put("created_at", createdAt)
        put("app_hint", appHint ?: JSONObject.NULL); put("attempts", attempts)
        put("last_error", lastError ?: JSONObject.NULL)
    }

    companion object {
        fun fromJson(o: JSONObject) = QueueItem(
            id = o.getString("id"),
            mode = o.optString("mode", "raw"),
            createdAt = o.optLong("created_at"),
            appHint = o.optString("app_hint").takeIf { !o.isNull("app_hint") && it.isNotEmpty() },
            attempts = o.optInt("attempts", 0),
            lastError = o.optString("last_error").takeIf { !o.isNull("last_error") && it.isNotEmpty() },
        )
    }
}

/** Outcome of one send attempt, decided by the caller (ApiClient wrapper). */
sealed class SendOutcome {
    data class Delivered(val payload: Any?) : SendOutcome()
    /** Transient (network down / 5xx / timeout): keep and stop draining for now. */
    data class Retry(val reason: String) : SendOutcome()
    /** Server rejected (4xx): keep the audio, record the reason, continue with the next item. */
    data class Rejected(val reason: String) : SendOutcome()
}

/**
 * File-backed FIFO. Each item = `<id>.wav` + `<id>.json`, written via temp-file + rename so a
 * crash never leaves a half item. Audio is deleted ONLY after a confirmed Delivered outcome:
 * a dictation is never lost.
 */
class DictationQueue(private val dir: File) {
    init { dir.mkdirs() }

    @Synchronized
    fun enqueue(wav: ByteArray, mode: String, appHint: String? = null,
                id: String = "utt_" + UUID.randomUUID().toString().replace("-", ""), now: Long = System.currentTimeMillis()): QueueItem {
        val item = QueueItem(id, mode, now, appHint)
        atomicWrite(wavFile(id), wav)
        atomicWrite(metaFile(id), item.toJson().toString().toByteArray())
        return item
    }

    @Synchronized
    fun list(): List<QueueItem> = (dir.listFiles { f -> f.name.endsWith(".json") } ?: emptyArray())
        .mapNotNull { f ->
            try { QueueItem.fromJson(JSONObject(f.readText())) } catch (_: Exception) { null }
        }
        .filter { wavFile(it.id).exists() }
        .sortedBy { it.createdAt }

    fun size(): Int = list().size

    fun audio(id: String): File = wavFile(id)

    @Synchronized
    fun remove(id: String) {
        wavFile(id).delete(); metaFile(id).delete()
    }

    @Synchronized
    fun markFailed(id: String, reason: String): QueueItem? {
        val f = metaFile(id)
        if (!f.exists()) return null
        val cur = QueueItem.fromJson(JSONObject(f.readText()))
        val next = cur.copy(attempts = cur.attempts + 1, lastError = reason)
        atomicWrite(f, next.toJson().toString().toByteArray())
        return next
    }

    /**
     * Sends queued items oldest-first. Stops at the first Retry (server unreachable) so we do
     * not hammer a dead link. Returns (delivered, remaining).
     */
    fun drain(onDelivered: (QueueItem, Any?) -> Unit = { _, _ -> },
              send: (QueueItem, File) -> SendOutcome): Pair<Int, Int> {
        var delivered = 0
        for (item in list()) {
            val outcome = try { send(item, wavFile(item.id)) } catch (e: Exception) {
                SendOutcome.Retry(e.message ?: e.javaClass.simpleName)
            }
            when (outcome) {
                is SendOutcome.Delivered -> { onDelivered(item, outcome.payload); remove(item.id); delivered++ }
                is SendOutcome.Retry -> { markFailed(item.id, outcome.reason); break }
                is SendOutcome.Rejected -> markFailed(item.id, outcome.reason)
            }
        }
        return delivered to size()
    }

    private fun wavFile(id: String) = File(dir, "$id.wav")
    private fun metaFile(id: String) = File(dir, "$id.json")

    private fun atomicWrite(target: File, bytes: ByteArray) {
        val tmp = File(dir, target.name + ".tmp")
        tmp.outputStream().use { it.write(bytes); it.fd.sync() }
        if (!tmp.renameTo(target)) { target.delete(); check(tmp.renameTo(target)) { "rename failed: ${target.name}" } }
    }
}
