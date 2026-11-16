package com.speakeasy.android

import android.os.Build
import android.util.Log
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.speakeasy.android.net.ApiClient
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * End-to-end on the real phone: bundled fixture WAV -> real ApiClient -> live SpeakEasy server
 * over the tailnet -> transcript. No UI, no input injection.
 *
 * Auth: uses the phone's stored device token if the operator has paired; otherwise pass a fresh
 * pairing code:  am instrument -w -e pairCode 123456 [-e serverUrl https://...] ...
 */
@RunWith(AndroidJUnit4::class)
class LiveServerE2ETest {
    private val inst = InstrumentationRegistry.getInstrumentation()
    private val ctx = inst.targetContext
    private val args = InstrumentationRegistry.getArguments()

    @Test
    fun fixtureWavTranscribesOnLiveServer() {
        val se = SpeakEasy.get(ctx)
        val url = args.getString("serverUrl") ?: se.settings.serverUrl
        args.getString("pairCode")?.let { code ->
            val r = ApiClient(url, { null }).pair(code, "${Build.MODEL} (e2e test)")
            se.settings.serverUrl = url
            r.deviceId?.let { se.settings.deviceId = it }
            se.secure.token = r.token
        }
        assumeTrue("Not paired and no pairCode argument given", se.secure.paired)

        val api = ApiClient(url, { se.secure.token })
        val h = api.health()
        Log.i(TAG, "health ok=${h.ok} ${h.latencyMs}ms")
        assertTrue("server not healthy: ${h.detail}", h.ok)

        val wav = File(ctx.cacheDir, "e2e-sample.wav")
        inst.context.assets.open("sample.wav").use { i -> wav.outputStream().use { i.copyTo(it) } }

        val t = api.transcribe(wav, se.settings.deviceId, "raw", appHint = "e2e-test")
        val result = "E2E_RESULT round_trip_ms=${t.roundTripMs} server_ms=${t.serverMs} " +
            "utterance_id=${t.utteranceId} text=\"${t.text}\""
        Log.i(TAG, result)
        inst.sendStatus(0, android.os.Bundle().apply { putString("stream", result + "\n") })
        val lower = t.text.lowercase()
        assertTrue("unexpected transcript: ${t.text}", listOf("quick", "brown", "fox", "dog").count { it in lower } >= 3)
        assertTrue("no utterance_id", t.utteranceId.isNotEmpty())
    }

    companion object { const val TAG = "SpeakEasyE2E" }
}
