package com.speakeasy.android.net

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** HTTP error from the SpeakEasy server (4xx/5xx). `transient` = worth retrying later. */
class ApiException(val code: Int, message: String) : IOException(message) {
    val transient: Boolean get() = code >= 500 || code == 408 || code == 429
}

data class Health(val ok: Boolean, val detail: String, val latencyMs: Long)
data class PairResult(val token: String, val deviceId: String?)
data class Transcript(
    val utteranceId: String,
    val text: String,
    val rawText: String,
    val mode: String,
    val rewritten: Boolean,
    val rewriteStatus: String,
    val serverMs: Long?,
    val roundTripMs: Long,
)

/**
 * Blocking client for the SpeakEasy tailnet API (call from Dispatchers.IO / a Worker).
 * Audio only ever goes to the operator's own server - no third-party endpoints.
 */
class ApiClient(
    baseUrl: String,
    private val tokenProvider: () -> String?,
    private val http: OkHttpClient = defaultHttp(),
) {
    val base: String = baseUrl.trim().trimEnd('/')

    private fun req(path: String) = Request.Builder().url(base + path).also { b ->
        tokenProvider()?.takeIf { it.isNotBlank() }?.let { b.header("Authorization", "Bearer $it") }
        b.header("Accept", "application/json")
    }

    private fun exec(r: Request): JSONObject {
        http.newCall(r).execute().use { resp -> return parse(resp) }
    }

    private fun parse(resp: Response): JSONObject {
        val body = resp.body?.string().orEmpty()
        if (!resp.isSuccessful) {
            val reason = try {
                val o = JSONObject(body)
                o.optString("detail").ifEmpty { o.optString("error") }.ifEmpty { o.optString("message") }
            } catch (_: Exception) { "" }
            throw ApiException(resp.code, "HTTP ${resp.code}" + (if (reason.isNotEmpty()) ": $reason" else ""))
        }
        return if (body.isBlank()) JSONObject() else try { JSONObject(body) } catch (_: Exception) {
            JSONObject().put("raw", body)
        }
    }

    fun health(): Health {
        val t0 = System.nanoTime()
        val o = exec(req("/v1/health").get().build())
        val ms = (System.nanoTime() - t0) / 1_000_000
        val ok = when {
            o.has("ok") -> o.optBoolean("ok")
            o.has("status") -> o.optString("status").lowercase() in setOf("ok", "healthy", "ready", "up")
            else -> true
        }
        return Health(ok, o.optString("engine").ifEmpty { o.optString("status") }, ms)
    }

    fun pair(code: String, deviceName: String): PairResult {
        val body = JSONObject().put("code", code.trim()).put("device_name", deviceName)
        val o = exec(Request.Builder().url("$base/v1/pair").header("Accept", "application/json")
            .post(body.toString().toRequestBody(JSON)).build())
        val token = firstString(o, "device_token", "token", "access_token")
            ?: throw ApiException(500, "Pair response had no token")
        return PairResult(token, firstString(o, "device_id"))
    }

    fun transcribe(wav: File, deviceId: String, mode: String, appHint: String? = null,
                   clientUtteranceId: String? = null): Transcript {
        val mb = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("audio", wav.name, wav.asRequestBody(WAV))
            .addFormDataPart("device_id", deviceId)
            .addFormDataPart("mode", mode)
        appHint?.let { mb.addFormDataPart("app_hint", it) }
        clientUtteranceId?.let { mb.addFormDataPart("utterance_id", it) }
        val t0 = System.nanoTime()
        val o = exec(req("/v1/transcribe").post(mb.build()).build())
        val rt = (System.nanoTime() - t0) / 1_000_000
        val text = o.optString("text")
        val raw = o.optString("raw_text").ifEmpty { text }
        val serverMs = when {
            o.has("latency_ms") -> o.optLong("latency_ms")
            o.has("total_ms") -> o.optLong("total_ms")
            o.has("stt_ms") -> o.optLong("stt_ms")
            else -> null
        }
        return Transcript(
            utteranceId = firstString(o, "utterance_id", "id") ?: "",
            text = text,
            rawText = raw,
            mode = o.optString("mode").ifEmpty { mode },
            rewriteStatus = o.optString("rewrite_status").ifEmpty { if (mode == "raw") "none" else "" },
            rewritten = when (o.optString("rewrite_status")) {
                "ok" -> true; "fallback", "none" -> false
                else -> mode != "raw" && text != raw
            },
            serverMs = serverMs,
            roundTripMs = rt,
        )
    }

    fun feedback(utteranceId: String, correctedText: String, deviceId: String? = null) {
        val b = JSONObject().put("utterance_id", utteranceId).put("corrected_text", correctedText)
        deviceId?.let { b.put("device_id", it) }
        exec(req("/v1/feedback").post(b.toString().toRequestBody(JSON)).build())
    }

    fun profile(): JSONObject = exec(req("/v1/profile").get().build())

    /** events: list of {type, ts, data, id} (contract v1). */
    fun events(deviceId: String, events: List<JSONObject>) {
        if (events.isEmpty()) return
        val b = JSONObject().put("device_id", deviceId).put("events", JSONArray(events))
        exec(req("/v1/events").post(b.toString().toRequestBody(JSON)).build())
    }

    companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
        val WAV = "audio/wav".toMediaType()

        fun defaultHttp(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(6, TimeUnit.SECONDS)
            .readTimeout(90, TimeUnit.SECONDS)   // rewrite modes can take a while
            .writeTimeout(60, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()

        private fun firstString(o: JSONObject, vararg keys: String): String? =
            keys.firstNotNullOfOrNull { k -> o.optString(k).takeIf { !o.isNull(k) && it.isNotEmpty() } }
    }
}
