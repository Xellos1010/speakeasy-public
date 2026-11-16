package com.speakeasy.android.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** 16 kHz mono PCM16 capture via AudioRecord with a live RMS level flow. */
class Recorder {
    private val _level = MutableStateFlow(0f)
    val level: StateFlow<Float> = _level

    @Volatile private var running = false
    private var thread: Thread? = null
    private val pcm = ByteArrayOutputStream()
    @Volatile private var error: String? = null

    val isRecording: Boolean get() = running

    /** Caller must hold RECORD_AUDIO. Throws IllegalStateException with a human reason on failure. */
    @SuppressLint("MissingPermission")
    fun start() {
        if (running) return
        val rate = WavEncoder.SAMPLE_RATE
        val minBuf = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) throw IllegalStateException("Microphone does not support 16 kHz mono")
        val bufSize = maxOf(minBuf, rate / 5 * 2) // >= 200 ms
        val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, rate,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufSize)
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            throw IllegalStateException("Microphone unavailable (in use by another app?)")
        }
        synchronized(pcm) { pcm.reset() }
        error = null
        rec.startRecording()
        running = true
        thread = Thread({
            val buf = ShortArray(rate / 20) // 50 ms frames
            val bb = ByteBuffer.allocate(buf.size * 2).order(ByteOrder.LITTLE_ENDIAN)
            try {
                while (running) {
                    val n = rec.read(buf, 0, buf.size)
                    if (n < 0) { error = "Microphone read error $n"; break }
                    if (n == 0) continue
                    bb.clear()
                    for (i in 0 until n) bb.putShort(buf[i])
                    synchronized(pcm) {
                        if (pcm.size() < MAX_BYTES) pcm.write(bb.array(), 0, n * 2)
                    }
                    _level.value = WavEncoder.rmsLevel(buf, n)
                }
            } finally {
                try { rec.stop() } catch (_: Exception) {}
                rec.release()
                _level.value = 0f
            }
        }, "speakeasy-recorder").apply { start() }
    }

    /** Stops capture and returns the complete WAV bytes (may be empty-data WAV). */
    fun stop(): ByteArray {
        running = false
        thread?.join(1500)
        thread = null
        val data = synchronized(pcm) { pcm.toByteArray() }
        return WavEncoder.encodePcmBytes(data)
    }

    fun cancel() { running = false; thread?.join(1500); thread = null; synchronized(pcm) { pcm.reset() } }

    companion object {
        /** 10 minutes of 16 kHz mono PCM16. */
        const val MAX_BYTES = 16_000 * 2 * 600
    }
}
