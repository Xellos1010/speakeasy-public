package com.speakeasy.android

import com.speakeasy.android.audio.WavEncoder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class WavEncoderTest {
    private fun le(b: ByteArray) = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN)

    @Test fun headerIsCanonical16kMonoPcm16() {
        val h = WavEncoder.header(32000)
        assertEquals(44, h.size)
        assertEquals("RIFF", String(h, 0, 4)); assertEquals("WAVE", String(h, 8, 4))
        assertEquals("fmt ", String(h, 12, 4)); assertEquals("data", String(h, 36, 4))
        val bb = le(h)
        assertEquals(36 + 32000, bb.getInt(4))
        assertEquals(16, bb.getInt(16)); assertEquals(1, bb.getShort(20).toInt())
        assertEquals(1, bb.getShort(22).toInt()); assertEquals(16000, bb.getInt(24))
        assertEquals(32000, bb.getInt(28)); assertEquals(2, bb.getShort(32).toInt())
        assertEquals(16, bb.getShort(34).toInt()); assertEquals(32000, bb.getInt(40))
    }

    @Test fun encodeSamplesLittleEndian() {
        val wav = WavEncoder.encode(shortArrayOf(1, -2, Short.MAX_VALUE))
        assertEquals(44 + 6, wav.size)
        val bb = le(wav)
        assertEquals(1, bb.getShort(44).toInt()); assertEquals(-2, bb.getShort(46).toInt())
        assertEquals(Short.MAX_VALUE, bb.getShort(48))
        assertEquals(6, bb.getInt(40))
    }

    @Test fun emptyRecordingStillValidWav() {
        val wav = WavEncoder.encodePcmBytes(ByteArray(0))
        assertEquals(44, wav.size); assertEquals(0, le(wav).getInt(40))
    }

    @Test fun durationAndLevel() {
        assertEquals(1000L, WavEncoder.durationMs(32000))
        assertEquals(0f, WavEncoder.rmsLevel(ShortArray(100)))
        val loud = ShortArray(100) { 30000 }
        assertTrue(WavEncoder.rmsLevel(loud) > 0.9f)
    }
}
