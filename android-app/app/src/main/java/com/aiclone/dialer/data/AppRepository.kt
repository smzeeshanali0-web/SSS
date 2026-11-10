package com.aiclone.dialer.data

import android.content.Context
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import org.json.JSONArray
import org.json.JSONObject

private val Context.dataStore by preferencesDataStore(name = "ai_clone_dialer")

class AppRepository(private val context: Context) {
    private val accountsKey = stringPreferencesKey("sip_accounts")
    private val defaultVoiceKey = stringPreferencesKey("default_voice")
    private val aiEnabledKey = booleanPreferencesKey("ai_enabled")
    private val backendUrlKey = stringPreferencesKey("backend_url")
    private val apiKeyKey = stringPreferencesKey("backend_api_key")
    private val elevenKeyKey = stringPreferencesKey("elevenlabs_api_key")
    private val historyKey = stringPreferencesKey("call_history")
    private val activeAccountKey = stringPreferencesKey("active_account")
    private val leadsKey = stringPreferencesKey("leads")
    private val scriptKey = stringPreferencesKey("agent_script")
    private val requireTestKey = booleanPreferencesKey("require_voice_test")

    val accounts: Flow<List<SipAccount>> = context.dataStore.data.map { decodeAccounts(it[accountsKey] ?: "[]") }
    val defaultVoice: Flow<String> = context.dataStore.data.map { it[defaultVoiceKey] ?: "" }
    val aiEnabled: Flow<Boolean> = context.dataStore.data.map { it[aiEnabledKey] ?: false }
    val activeAccount: Flow<String> = context.dataStore.data.map { it[activeAccountKey] ?: "" }
    val backend: Flow<BackendSettings> = context.dataStore.data.map { BackendSettings(it[backendUrlKey] ?: "", it[apiKeyKey] ?: "", it[elevenKeyKey] ?: "") }
    val history: Flow<List<CallRecord>> = context.dataStore.data.map { decodeHistory(it[historyKey] ?: "[]") }
    val script: Flow<String> = context.dataStore.data.map { it[scriptKey] ?: DEFAULT_SCRIPT }
    val requireTest: Flow<Boolean> = context.dataStore.data.map { it[requireTestKey] ?: true }
    val leads: Flow<List<Lead>> = context.dataStore.data.map { decodeLeads(it[leadsKey] ?: "[]") }

    suspend fun saveAccount(account: SipAccount) {
        context.dataStore.edit { prefs ->
            val list = decodeAccounts(prefs[accountsKey] ?: "[]").toMutableList()
            val idx = list.indexOfFirst { it.id == account.id }
            if (idx >= 0) list[idx] = account else list.add(account)
            prefs[accountsKey] = encodeAccounts(list)
        }
    }
    suspend fun deleteAccount(id: String) {
        context.dataStore.edit { prefs ->
            prefs[accountsKey] = encodeAccounts(decodeAccounts(prefs[accountsKey] ?: "[]").filter { it.id != id })
        }
    }
    suspend fun setActiveAccount(id: String) { context.dataStore.edit { it[activeAccountKey] = id } }
    suspend fun setDefaultVoice(id: String) { context.dataStore.edit { it[defaultVoiceKey] = id } }
    suspend fun setAiEnabled(enabled: Boolean) { context.dataStore.edit { it[aiEnabledKey] = enabled } }
    suspend fun saveBackend(s: BackendSettings) {
        context.dataStore.edit { it[backendUrlKey] = s.url.trim(); it[apiKeyKey] = s.apiKey.trim(); it[elevenKeyKey] = s.elevenKey.trim() }
    }
    suspend fun addHistory(r: CallRecord) {
        context.dataStore.edit { prefs ->
            val list = (listOf(r) + decodeHistory(prefs[historyKey] ?: "[]")).take(500)
            prefs[historyKey] = encodeHistory(list)
        }
    }
    suspend fun updateHistory(id: String, disposition: String, note: String) {
        context.dataStore.edit { prefs ->
            val list = decodeHistory(prefs[historyKey] ?: "[]").map { if (it.id == id) it.copy(disposition = disposition, note = note) else it }
            prefs[historyKey] = encodeHistory(list)
        }
    }
    suspend fun updateLeads(transform: (List<Lead>) -> List<Lead>) {
        context.dataStore.edit { prefs -> prefs[leadsKey] = encodeLeads(transform(decodeLeads(prefs[leadsKey] ?: "[]"))) }
    }
    suspend fun setScript(text: String) { context.dataStore.edit { it[scriptKey] = text } }
    suspend fun setRequireTest(on: Boolean) { context.dataStore.edit { it[requireTestKey] = on } }
    suspend fun clearHistory() { context.dataStore.edit { it[historyKey] = "[]" } }

    private fun encodeHistory(list: List<CallRecord>) = JSONArray().apply {
        list.forEach { h -> put(JSONObject().apply {
            put("id", h.id); put("number", h.number); put("startedAt", h.startedAt)
            put("seconds", h.seconds); put("aiVoice", h.aiVoice); put("result", h.result)
            put("disposition", h.disposition); put("note", h.note)
        }) }
    }.toString()

    private fun decodeHistory(raw: String): List<CallRecord> = runCatching {
        val arr = JSONArray(raw)
        (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            CallRecord(o.optString("id"), o.optString("number"), o.optLong("startedAt"), o.optInt("seconds"),
                o.optBoolean("aiVoice"), o.optString("result"), o.optString("disposition"), o.optString("note"))
        }
    }.getOrDefault(emptyList())

    private fun encodeLeads(list: List<Lead>) = JSONArray().apply {
        list.forEach { l -> put(JSONObject().apply {
            put("id", l.id); put("name", l.name); put("phone", l.phone); put("status", l.status)
            put("notes", l.notes); put("attempts", l.attempts); put("lastCallAt", l.lastCallAt); put("callbackAt", l.callbackAt)
        }) }
    }.toString()

    private fun decodeLeads(raw: String): List<Lead> = runCatching {
        val arr = JSONArray(raw)
        (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Lead(o.optString("id"), o.optString("name"), o.optString("phone"), o.optString("status", "NEW"),
                o.optString("notes"), o.optInt("attempts"), o.optLong("lastCallAt"), o.optLong("callbackAt"))
        }
    }.getOrDefault(emptyList())

    private fun encodeAccounts(items: List<SipAccount>) = JSONArray().apply {
        items.forEach { a -> put(JSONObject().apply {
            put("id", a.id); put("label", a.label); put("username", a.username); put("password", a.password)
            put("server", a.server); put("domain", a.domain); put("ipAddress", a.ipAddress); put("port", a.port)
            put("proxy", a.proxy); put("authId", a.authId); put("callerId", a.callerId)
            put("displayName", a.displayName); put("transport", a.transport); put("stun", a.stun); put("enabled", a.enabled); put("agentMode", a.agentMode)
        }) }
    }.toString()

    private fun decodeAccounts(raw: String): List<SipAccount> = runCatching {
        val arr = JSONArray(raw)
        (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            SipAccount(id = o.optString("id"), label = o.optString("label"), username = o.optString("username"),
                password = o.optString("password"), server = o.optString("server"), domain = o.optString("domain"),
                ipAddress = o.optString("ipAddress"), port = o.optInt("port", 5060), proxy = o.optString("proxy"),
                authId = o.optString("authId"), callerId = o.optString("callerId"), displayName = o.optString("displayName"),
                transport = o.optString("transport", "UDP"), stun = o.optString("stun"), enabled = o.optBoolean("enabled", true), agentMode = o.optBoolean("agentMode", false))
        }
    }.getOrDefault(emptyList())
}
