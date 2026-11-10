package com.aiclone.dialer.audio

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

object VoiceTools {
    const val RATE = 16000

    /** PCM16 mono -> WAV file bytes. */
    fun wav(pcm: ByteArray, rate: Int = RATE): ByteArray {
        val bb = ByteBuffer.allocate(44 + pcm.size).order(ByteOrder.LITTLE_ENDIAN)
        bb.put("RIFF".toByteArray()); bb.putInt(36 + pcm.size); bb.put("WAVE".toByteArray())
        bb.put("fmt ".toByteArray()); bb.putInt(16); bb.putShort(1.toShort()); bb.putShort(1.toShort())
        bb.putInt(rate); bb.putInt(rate * 2); bb.putShort(2.toShort()); bb.putShort(16.toShort())
        bb.put("data".toByteArray()); bb.putInt(pcm.size); bb.put(pcm)
        return bb.array()
    }

    /** Plays PCM16 16 kHz mono and returns when finished. */
    suspend fun playPcm(pcm: ByteArray, rate: Int = RATE) = withContext(Dispatchers.IO) {
        if (pcm.size < 2) return@withContext
        val size = pcm.size - pcm.size % 2
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(rate).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(size).setTransferMode(AudioTrack.MODE_STATIC).build()
        try {
            track.write(pcm, 0, size)
            track.play()
            delay(size / (rate * 2 / 1000L) + 300)
        } finally {
            runCatching { track.stop() }
            track.release()
        }
    }
}

/** Records PCM16 16 kHz mono from the microphone until stop() is called or maxSeconds is reached. */
class VoiceRecorder {
    @Volatile private var recording = false

    @SuppressLint("MissingPermission")
    suspend fun record(maxSeconds: Int, onSecond: (Int) -> Unit = {}): ByteArray = withContext(Dispatchers.IO) {
        val rate = VoiceTools.RATE
        val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(MediaRecorder.AudioSource.MIC, rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(min, 32000))
        if (rec.state != AudioRecord.STATE_INITIALIZED) { rec.release(); throw IllegalStateException("Microphone unavailable") }
        val out = ByteArrayOutputStream()
        val buf = ByteArray(3200)
        val limit = rate * 2 * maxSeconds
        var total = 0
        var lastSec = -1
        recording = true
        rec.startRecording()
        try {
            while (recording && total < limit) {
                val n = rec.read(buf, 0, buf.size)
                if (n <= 0) break
                out.write(buf, 0, n)
                total += n
                val sec = total / (rate * 2)
                if (sec != lastSec) { lastSec = sec; onSecond(sec) }
            }
        } finally {
            recording = false
            runCatching { rec.stop() }
            rec.release()
        }
        out.toByteArray()
    }

    fun stop() { recording = false }
}
