package com.aiclone.dialer.data

import java.util.UUID

data class SipAccount(
    val id: String = UUID.randomUUID().toString(),
    val label: String,
    val username: String,
    val password: String,
    val server: String,
    val domain: String = "",
    val ipAddress: String = "",
    val port: Int = 5060,
    val proxy: String = "",
    val authId: String = "",
    val callerId: String = "",
    val displayName: String = "",
    val transport: String = "UDP",
    val stun: String = "",
    val enabled: Boolean = true,
    /** VICIdial / Asterisk agent phone: register, wait for the server to call this extension, auto-answer. */
    val agentMode: Boolean = false
)

/** Voice profile as reported by the backend (backend is the source of truth). */
data class VoiceProfile(
    val id: String,
    val name: String,
    val ownerName: String,
    val ownerRelation: String,
    val status: String,   // awaiting_consent | consented | cloning | ready | failed
    val error: String = ""
)

data class CallRecord(
    val id: String = UUID.randomUUID().toString(),
    val number: String,
    val startedAt: Long,
    val seconds: Int,
    val aiVoice: Boolean,
    val result: String,
    val disposition: String = "",
    val note: String = ""
)

/** A lead (contact) in the agent's list, VICIdial style. status = NEW or a disposition code. */
data class Lead(
    val id: String = UUID.randomUUID().toString(),
    val name: String,
    val phone: String,
    val status: String = "NEW",
    val notes: String = "",
    val attempts: Int = 0,
    val lastCallAt: Long = 0L,
    /** Scheduled call-back time (epoch ms); 0 = none. Set when the disposition is CALLBK. */
    val callbackAt: Long = 0L
)

/** VICIdial-style dispositions: code to label. */
val DISPOSITIONS = listOf(
    "SALE" to "Sale", "CALLBK" to "Call back", "NI" to "Not interested", "NA" to "No answer",
    "B" to "Busy", "AA" to "Answering machine", "WRONG" to "Wrong number", "DNC" to "Do not call",
    "DROP" to "Dropped", "OTHER" to "Other"
)

fun dispositionLabel(code: String) = DISPOSITIONS.firstOrNull { it.first == code }?.second ?: code

fun digitsOf(s: String) = s.filter { it.isDigit() }
fun sameNumber(a: String, b: String): Boolean {
    val x = digitsOf(a).takeLast(10); val y = digitsOf(b).takeLast(10)
    return x.length >= 6 && x == y
}

data class BackendSettings(val url: String = "", val apiKey: String = "", val elevenKey: String = "")

/** VICIdial-style agent pause codes. */
val PAUSE_CODES = listOf("BREAK" to "Break", "LUNCH" to "Lunch", "LAGGED" to "Lagged / tech issue", "MEETING" to "Meeting", "LOGIN" to "Login / setup")

/** Quick call-back choices: label to delay in milliseconds. */
val CALLBACK_CHOICES = listOf("In 1 hour" to 3_600_000L, "In 3 hours" to 10_800_000L, "Tomorrow" to 86_400_000L, "In 3 days" to 259_200_000L, "Next week" to 604_800_000L)

const val DEFAULT_SCRIPT = "Hello {name}, my name is ____ calling from ____.\n\n" +
    "Purpose of call: ____\n\nKey points:\n- ____\n- ____\n\nIf not interested: thank them and set disposition NI.\nIf asked to call later: set CALLBK and pick a time."

/** Fills {name} {phone} {first} in an agent script (VICIdial-style script tags). */
fun fillScript(script: String, lead: Lead?, number: String): String {
    val name = lead?.let { l -> l.name.takeIf { it.isNotBlank() && it != l.phone } } ?: ""
    val first = name.substringBefore(' ')
    return script.replace("{name}", name.ifEmpty { "there" }).replace("{first}", first.ifEmpty { "there" })
        .replace("{phone}", lead?.phone ?: number)
}
