package com.aiclone.dialer.call

import android.annotation.SuppressLint
import android.content.Context
import android.media.*
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import com.aiclone.dialer.data.CallRecord
import com.aiclone.dialer.data.SipAccount
import com.aiclone.dialer.net.BackendClient
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import kotlin.concurrent.thread

data class CallUi(
    val state: String = "idle",          // idle | connecting | registering | calling | ringing | connected | ended
    val detail: String = "",
    val number: String = "",
    val aiVoice: Boolean = false,
    val muted: Boolean = false,
    val speaker: Boolean = false,
    val seconds: Int = 0,
    val message: String = ""             // last warning / error shown to the user
)

/**
 * Phone call through the backend SIP gateway: mic PCM16 16 kHz -> websocket -> (AI voice conversion) -> SIP/RTP,
 * and remote audio back. If AI voice is requested and anything fails the backend refuses/silences; this app never
 * sends the raw mic as a fallback to an AI-voice call.
 */
class CallClient(private val context: Context, private val scope: CoroutineScope) {
    val ui = MutableStateFlow(CallUi())
    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var ws: WebSocket? = null
    private var rec: AudioRecord? = null
    private var track: AudioTrack? = null
    @Volatile private var running = false
    private var timer: Job? = null
    private var startedAt = 0L
    private var finished: ((CallRecord) -> Unit)? = null
    private var recorded = false

    val inCall get() = ui.value.state !in listOf("idle", "ended")

    fun start(backend: BackendClient, account: SipAccount, number: String, profileId: String?, onFinished: (CallRecord) -> Unit) {
        val agent = account.agentMode
        if (inCall) return
        finished = onFinished; recorded = false; startedAt = System.currentTimeMillis()
        ui.value = CallUi(state = "connecting", number = if (agent) "VICIdial session" else number, aiVoice = profileId != null)
        CallService.start(context, if (agent) "VICIdial session" else number)   // foreground service: keeps mic + network alive until the call ends
        val start = JSONObject().put("type", "start").put("mode", if (agent) "agent" else "dial").put("destination", number)
            .put("elevenlabs_key", backend.elevenKey).put("profile_id", profileId ?: JSONObject.NULL).put("disclosure_ack", profileId != null)
            .put("account", JSONObject().put("username", account.username).put("password", account.password)
                .put("server", account.server).put("port", account.port).put("domain", account.domain)
                .put("auth_id", account.authId).put("display_name", account.displayName)
                .put("caller_id", account.callerId).put("proxy", account.proxy).put("transport", account.transport))
        ws = backend.newWebSocket(object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) { webSocket.send(start.toString()) }
            override fun onMessage(webSocket: WebSocket, text: String) = handleJson(JSONObject(text))
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) { track?.write(bytes.toByteArray(), 0, bytes.size) }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { finish("Connection error: ${t.message}") }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { finish(ui.value.detail.ifEmpty { "closed" }) }
        })
    }

    private fun handleJson(o: JSONObject) {
        when (o.optString("type")) {
            "state" -> {
                val st = o.optString("state"); val detail = o.optString("detail")
                ui.value = ui.value.copy(state = st, detail = detail)
                if (st == "connected") startAudio()
                if (st == "ended") finish(detail.ifEmpty { "ended" })
            }
            "error", "warning" -> ui.value = ui.value.copy(message = o.optString("message"))
            "muted" -> ui.value = ui.value.copy(muted = o.optBoolean("on"))
        }
    }

    @SuppressLint("MissingPermission")
    private fun startAudio() {
        if (running) return
        audio.mode = AudioManager.MODE_IN_COMMUNICATION
        val rate = 16000
        val minIn = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val r = try {
            AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, rate, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT, maxOf(minIn, 6400))
        } catch (e: SecurityException) { ui.value = ui.value.copy(message = "Microphone permission denied"); return }
        if (r.state != AudioRecord.STATE_INITIALIZED) { ui.value = ui.value.copy(message = "Microphone unavailable"); return }
        if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(r.audioSessionId)?.enabled = true
        if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(r.audioSessionId)?.enabled = true
        val minOut = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(rate).setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(maxOf(minOut, 6400) * 2).setTransferMode(AudioTrack.MODE_STREAM).build().also { it.play() }
        rec = r; running = true; r.startRecording()
        thread(name = "mic", isDaemon = true) {
            val buf = ByteArray(640) // 20 ms
            while (running) {
                var off = 0
                while (off < buf.size && running) {
                    val n = r.read(buf, off, buf.size - off)
                    if (n <= 0) break
                    off += n
                }
                val sock = ws
                if (off == buf.size && !ui.value.muted && sock != null && sock.queueSize() < 64_000) sock.send(buf.toByteString())
            }
        }
        timer = scope.launch { while (isActive) { delay(1000); ui.value = ui.value.copy(seconds = ui.value.seconds + 1) } }
    }

    fun reset() { if (!inCall) ui.value = CallUi() }
    fun hangup() { ws?.send(JSONObject().put("type", "hangup").toString()) }
    fun dtmf(d: Char) { ws?.send(JSONObject().put("type", "dtmf").put("digit", d.toString()).toString()) }
    fun setMuted(m: Boolean) { ws?.send(JSONObject().put("type", "mute").put("on", m).toString()); ui.value = ui.value.copy(muted = m) }
    fun setSpeaker(on: Boolean) {
        @Suppress("DEPRECATION") audio.isSpeakerphoneOn = on
        ui.value = ui.value.copy(speaker = on)
    }

    private fun finish(result: String) {
        if (recorded) return
        recorded = true
        running = false
        timer?.cancel()
        runCatching { rec?.stop(); rec?.release() }; rec = null
        runCatching { track?.stop(); track?.release() }; track = null
        @Suppress("DEPRECATION") audio.isSpeakerphoneOn = false
        audio.mode = AudioManager.MODE_NORMAL
        runCatching { ws?.close(1000, "bye") }; ws = null
        CallService.stop(context)
        val s = ui.value
        ui.value = s.copy(state = "ended", detail = result)
        finished?.invoke(CallRecord(number = s.number, startedAt = startedAt, seconds = s.seconds, aiVoice = s.aiVoice, result = result))
    }
}
