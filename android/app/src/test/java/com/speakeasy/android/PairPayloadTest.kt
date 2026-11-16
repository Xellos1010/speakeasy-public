package com.speakeasy.android

import com.speakeasy.android.data.PairPayload
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairPayloadTest {
    @Test fun contractJsonQr() {
        val p = PairPayload.parse("""{"v":1,"type":"speakeasy-pair","url":"https://host.example.net:8445","code":"123456","device":"pixel"}""")!!
        assertEquals("123456", p.code); assertEquals("https://host.example.net:8445", p.url); assertEquals("pixel", p.device)
    }
    @Test fun deepLink() {
        val p = PairPayload.parse("speakeasy://pair?code=654321&url=https%3A%2F%2Fhost.example.net%3A8445")!!
        assertEquals("654321", p.code); assertEquals("https://host.example.net:8445", p.url)
    }
    @Test fun bareCodeAndRejects() {
        assertEquals("111222", PairPayload.parse(" 111 222 ")!!.code)
        assertNull(PairPayload.parse("""{"type":"other","code":"123456"}"""))
        assertNull(PairPayload.parse("12345"))
        assertNull(PairPayload.parse("https://example.com"))
    }
}
