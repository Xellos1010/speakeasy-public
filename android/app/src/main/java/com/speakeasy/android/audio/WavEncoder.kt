package com.speakeasy.android.audio

import java.nio.ByteBuffer
import java.nio.ByteOrder

/** Canonical 44-byte RIFF/WAVE PCM encoder (little-endian). Pure Kotlin, unit-tested. */
object WavEncoder {
    const val SAMPLE_RATE = 16_000
    const val CHANNELS = 1
    const val BITS = 16

    fun header(dataLen: Int, sampleRate: Int = SAMPLE_RATE, channels: Int = CHANNELS, bits: Int = BITS): ByteArray {
        require(dataLen >= 0) { "dataLen must be >= 0" }
        val byteRate = sampleRate * channels * bits / 8
        val blockAlign = channels * bits / 8
        return ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray(Charsets.US_ASCII))
            putInt(36 + dataLen)
            put("WAVE".toByteArray(Charsets.US_ASCII))
            put("fmt ".toByteArray(Charsets.US_ASCII))
            putInt(16)                 // PCM fmt chunk size
            putShort(1)                // audio format = PCM
            putShort(channels.toShort())
            putInt(sampleRate)
            putInt(byteRate)
            putShort(blockAlign.toShort())
            putShort(bits.toShort())
            put("data".toByteArray(Charsets.US_ASCII))
            putInt(dataLen)
        }.array()
    }

    /** PCM16 little-endian bytes -> full WAV file bytes. */
    fun encodePcmBytes(pcm: ByteArray, sampleRate: Int = SAMPLE_RATE): ByteArray =
        header(pcm.size, sampleRate) + pcm

    /** PCM16 samples -> full WAV file bytes. */
    fun encode(samples: ShortArray, sampleRate: Int = SAMPLE_RATE): ByteArray {
        val bb = ByteBuffer.allocate(samples.size * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (s in samples) bb.putShort(s)
        return encodePcmBytes(bb.array(), sampleRate)
    }

    /** Duration in ms of a mono PCM16 payload. */
    fun durationMs(pcmBytes: Int, sampleRate: Int = SAMPLE_RATE): Long =
        pcmBytes.toLong() * 1000L / (sampleRate * 2L)

    /** Normalised RMS level 0..1 of a PCM16 buffer (for the live meter). */
    fun rmsLevel(buf: ShortArray, n: Int = buf.size): Float {
        if (n <= 0) return 0f
        var sum = 0.0
        for (i in 0 until n) { val v = buf[i].toDouble(); sum += v * v }
        val rms = Math.sqrt(sum / n) / 32768.0
        // perceptual-ish scaling so normal speech sits mid-meter
        return (Math.sqrt(rms) * 1.6).toFloat().coerceIn(0f, 1f)
    }
}
