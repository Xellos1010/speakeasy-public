package com.speakeasy.android

import com.speakeasy.android.audio.WavEncoder
import com.speakeasy.android.net.ApiClient
import com.speakeasy.android.net.ApiException
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class ApiClientTest {
    @get:Rule val tmp = TemporaryFolder()
    private lateinit var server: MockWebServer
    private lateinit var api: ApiClient

    @Before fun setUp() {
        server = MockWebServer(); server.start()
        api = ApiClient(server.url("/").toString(), { "tok123" })
    }
    @After fun tearDown() { server.shutdown() }

    @Test fun healthSendsBearer() {
        server.enqueue(MockResponse().setBody("""{"ok":true,"engine":"parakeet"}"""))
        val h = api.health()
        assertTrue(h.ok); assertEquals("parakeet", h.detail)
        val r = server.takeRequest()
        assertEquals("/v1/health", r.path); assertEquals("Bearer tok123", r.getHeader("Authorization"))
    }

    @Test fun pairPostsCodeWithoutAuthAndReadsToken() {
        server.enqueue(MockResponse().setBody("""{"device_token":"dt","device_id":"dev-1"}"""))
        val p = ApiClient(server.url("/").toString(), { null }).pair("123456", "Note10")
        assertEquals("dt", p.token); assertEquals("dev-1", p.deviceId)
        val r = server.takeRequest()
        assertEquals("POST", r.method); assertNull(r.getHeader("Authorization"))
        val body = JSONObject(r.body.readUtf8())
        assertEquals("123456", body.getString("code")); assertEquals("Note10", body.getString("device_name"))
    }

    @Test fun transcribeMultipartAndParse() {
        server.enqueue(MockResponse().setBody(
            """{"utterance_id":"u1","text":"Hello, world.","raw_text":"hello world","mode":"clean","latency_ms":412,"rewrite_status":"ok"}"""))
        val wav = tmp.newFile("a.wav").apply { writeBytes(WavEncoder.encode(ShortArray(160))) }
        val t = api.transcribe(wav, "dev-1", "clean", appHint = "com.slack")
        assertEquals("u1", t.utteranceId); assertEquals("Hello, world.", t.text)
        assertEquals("hello world", t.rawText); assertTrue(t.rewritten)
        val r = server.takeRequest()
        assertEquals("/v1/transcribe", r.path)
        assertTrue(r.getHeader("Content-Type")!!.startsWith("multipart/form-data"))
        val body = r.body.readUtf8()
        assertTrue(body.contains("name=\"audio\"")); assertTrue(body.contains("RIFF"))
        assertTrue(body.contains("name=\"device_id\"")); assertTrue(body.contains("dev-1"))
        assertTrue(body.contains("name=\"mode\"")); assertTrue(body.contains("com.slack"))
    }

    @Test fun rawModeNotMarkedRewritten() {
        server.enqueue(MockResponse().setBody("""{"utterance_id":"u2","text":"hi","raw_text":"hi"}"""))
        val wav = tmp.newFile("b.wav").apply { writeBytes(WavEncoder.encode(ShortArray(16))) }
        assertFalse(api.transcribe(wav, "d", "raw").rewritten)
    }

    @Test fun errorsCarryCodeAndTransience() {
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"detail":"bad token"}"""))
        try { api.health(); fail() } catch (e: ApiException) {
            assertEquals(401, e.code); assertFalse(e.transient); assertTrue(e.message!!.contains("bad token"))
        }
        server.enqueue(MockResponse().setResponseCode(503))
        try { api.health(); fail() } catch (e: ApiException) { assertTrue(e.transient) }
    }

    @Test fun feedbackAndEvents() {
        server.enqueue(MockResponse().setBody("{}")); server.enqueue(MockResponse().setBody("{}"))
        api.feedback("u1", "Corrected.", "dev-1")
        val f = JSONObject(server.takeRequest().body.readUtf8())
        assertEquals("u1", f.getString("utterance_id")); assertEquals("Corrected.", f.getString("corrected_text"))
        api.events("dev-1", listOf(JSONObject().put("type", "mode_choice").put("mode", "email")))
        val ev = server.takeRequest()
        assertEquals("/v1/events", ev.path)
        assertEquals("mode_choice", JSONObject(ev.body.readUtf8()).getJSONArray("events").getJSONObject(0).getString("type"))
    }

    @Test(expected = java.io.IOException::class)
    fun unreachableServerThrowsIOException() {
        server.shutdown()
        api.health()
    }
}
