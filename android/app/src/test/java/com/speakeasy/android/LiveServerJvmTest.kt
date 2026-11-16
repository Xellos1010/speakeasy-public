package com.speakeasy.android

import com.speakeasy.android.net.ApiClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * Contract check of the real Kotlin client against the LIVE server (from the workstation JVM).
 * Skipped unless SPK_PAIR_CODE (a fresh one-time code from `speakeasy pair`) is in the env.
 */
class LiveServerJvmTest {
    @get:Rule val tmp = TemporaryFolder()

    @Test fun pairTranscribeFeedbackEventsProfile() {
        val code = System.getenv("SPK_PAIR_CODE"); assumeTrue(!code.isNullOrBlank())
        val url = System.getenv("SPK_URL") ?: BuildConfig.DEFAULT_SERVER_URL
        val p = ApiClient(url, { null }).pair(code!!, "android-jvm-contract-test")
        assertTrue(p.deviceId!!.startsWith("dev_"))
        val api = ApiClient(url, { p.token })
        assertTrue(api.health().ok)
        val wav = tmp.newFile("s.wav").apply { writeBytes(java.io.File("src/test/resources/sample.wav").readBytes()) }
        val uid = "utt_" + java.util.UUID.randomUUID().toString().replace("-", "")
        val t = api.transcribe(wav, p.deviceId!!, "raw", "jvm-test", uid)
        println("LIVE_JVM round_trip_ms=${t.roundTripMs} server_ms=${t.serverMs} rewrite=${t.rewriteStatus} text=\"${t.text}\"")
        assertTrue(listOf("quick", "brown", "fox", "dog").count { it in t.text.lowercase() } >= 3)
        assertEquals(uid, t.utteranceId)
        val again = api.transcribe(wav, p.deviceId!!, "raw", "jvm-test", uid) // idempotent retry
        assertEquals(t.text, again.text)
        api.feedback(t.utteranceId, t.text, p.deviceId)
        api.events(p.deviceId!!, listOf(org.json.JSONObject().put("type", "mode_choice").put("ts", System.currentTimeMillis())
            .put("id", "and_" + uid).put("data", org.json.JSONObject().put("mode", "raw"))))
        assertTrue(api.profile().has("version"))
    }
}
