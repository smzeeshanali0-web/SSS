@file:OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)

package com.aiclone.dialer

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.aiclone.dialer.audio.VoiceRecorder
import com.aiclone.dialer.audio.VoiceTools
import com.aiclone.dialer.call.CallClient
import com.aiclone.dialer.call.CallUi
import com.aiclone.dialer.data.*
import com.aiclone.dialer.net.BackendClient
import com.aiclone.dialer.net.BackendException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.text.SimpleDateFormat
import java.util.*

private val ViciScheme = darkColorScheme(
    primary = Color(0xFF4FC3F7), onPrimary = Color(0xFF00212E),
    secondary = Color(0xFF81C784), onSecondary = Color(0xFF00210A),
    background = Color(0xFF0B1620), onBackground = Color(0xFFE3EEF5),
    surface = Color(0xFF12222F), onSurface = Color(0xFFE3EEF5),
    surfaceVariant = Color(0xFF1B3042), onSurfaceVariant = Color(0xFFB8CCD9),
    error = Color(0xFFEF5350)
)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val repository = AppRepository(applicationContext)
        val callClient = CallClient(applicationContext, lifecycleScope)
        setContent { MaterialTheme(colorScheme = ViciScheme) { Surface(color = MaterialTheme.colorScheme.background) { DialerApp(repository, callClient) } } }
    }
}

private fun toast(c: Context, m: String) = Toast.makeText(c, m, Toast.LENGTH_LONG).show()
private fun fmt(sec: Int) = "%02d:%02d".format(sec / 60, sec % 60)
private fun fmtLong(sec: Int) = if (sec >= 3600) "%d:%02d:%02d".format(sec / 3600, sec % 3600 / 60, sec % 60) else fmt(sec)
private fun startOfToday(): Long = Calendar.getInstance().apply {
    set(Calendar.HOUR_OF_DAY, 0); set(Calendar.MINUTE, 0); set(Calendar.SECOND, 0); set(Calendar.MILLISECOND, 0)
}.timeInMillis
private fun when_(ms: Long) = SimpleDateFormat("dd MMM HH:mm", Locale.getDefault()).format(Date(ms))

private fun readPicked(c: Context, uri: Uri): Triple<String, ByteArray, String> {
    var name = "voice.mp3"
    c.contentResolver.query(uri, null, null, null, null)?.use {
        if (it.moveToFirst()) { val i = it.getColumnIndex(OpenableColumns.DISPLAY_NAME); if (i >= 0) name = it.getString(i) }
    }
    val bytes = c.contentResolver.openInputStream(uri)?.use { s -> s.readBytes() } ?: ByteArray(0)
    return Triple(name, bytes, c.contentResolver.getType(uri) ?: "audio/mpeg")
}

private fun parseLeads(text: String): List<Lead> = text.lineSequence().mapNotNull { line ->
    val parts = line.split(',', ';', '\t').map { it.trim().trim('"') }.filter { it.isNotEmpty() }
    val phone = parts.firstOrNull { p -> p.count { it.isDigit() } >= 6 } ?: return@mapNotNull null
    val name = parts.firstOrNull { it != phone }.orEmpty()
    Lead(name = name.ifEmpty { phone }, phone = phone)
}.toList()

private data class Pending(val id: String, val ownerName: String, val relation: String, val text: String)

/** Returns a function that runs an action once the microphone permission is granted (asks if needed). */
@Composable
private fun rememberMicGate(): (() -> Unit) -> Unit {
    val ctx = LocalContext.current
    var pending by remember { mutableStateOf<(() -> Unit)?>(null) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        val p = pending; pending = null
        if (ok) p?.invoke() else toast(ctx, "Microphone permission is required")
    }
    return { action ->
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) action()
        else { pending = action; launcher.launch(Manifest.permission.RECORD_AUDIO) }
    }
}

@Composable
private fun DialerApp(repo: AppRepository, calls: CallClient) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val accounts by repo.accounts.collectAsState(emptyList())
    val defaultVoice by repo.defaultVoice.collectAsState("")
    val aiEnabled by repo.aiEnabled.collectAsState(false)
    val activeAccount by repo.activeAccount.collectAsState("")
    val backendSettings by repo.backend.collectAsState(BackendSettings())
    val history by repo.history.collectAsState(emptyList())
    val leads by repo.leads.collectAsState(emptyList())
    val callUi by calls.ui.collectAsState()
    val backend = remember(backendSettings) { BackendClient(backendSettings.url, backendSettings.apiKey, backendSettings.elevenKey) }

    var tab by remember { mutableStateOf("Dialer") }
    var number by remember { mutableStateOf("") }
    var profiles by remember { mutableStateOf(emptyList<VoiceProfile>()) }
    var busy by remember { mutableStateOf("") }
    var editAccount by remember { mutableStateOf<SipAccount?>(null) }
    var showAccountDialog by remember { mutableStateOf(false) }
    var showAddVoice by remember { mutableStateOf(false) }
    var pending by remember { mutableStateOf<Pending?>(null) }
    var confirmAi by remember { mutableStateOf(false) }
    var wantCall by remember { mutableStateOf(false) }
    var selectedLeadId by remember { mutableStateOf("") }
    var lastRecord by remember { mutableStateOf<CallRecord?>(null) }
    var testProfile by remember { mutableStateOf<VoiceProfile?>(null) }
    var editLead by remember { mutableStateOf<Lead?>(null) }
    var showLeadDialog by remember { mutableStateOf(false) }
    var showImport by remember { mutableStateOf(false) }
    val script by repo.script.collectAsState(DEFAULT_SCRIPT)
    val requireTest by repo.requireTest.collectAsState(true)
    var paused by remember { mutableStateOf(false) }
    var pauseCode by remember { mutableStateOf("") }
    var statusSince by remember { mutableStateOf(System.currentTimeMillis()) }
    var needTest by remember { mutableStateOf(false) }
    var tested by remember { mutableStateOf(setOf<String>()) }

    fun <T> guarded(label: String, block: suspend () -> T, done: (T) -> Unit = {}) {
        scope.launch {
            busy = label
            try { done(block()) } catch (e: BackendException) { toast(ctx, e.message ?: "Error") }
            catch (e: Exception) { toast(ctx, "Error: ${e.message}") }
            busy = ""
        }
    }
    fun refresh() = guarded("Refreshing voices…", { backend.profiles() }, { profiles = it })

    LaunchedEffect(backendSettings) { if (backendSettings.url.isNotBlank()) runCatching { profiles = backend.profiles() } }

    val acc = accounts.firstOrNull { it.id == activeAccount && it.enabled } ?: accounts.firstOrNull { it.enabled }
    val readyVoices = profiles.filter { it.status == "ready" }
    val currentVoice = profiles.firstOrNull { it.id == defaultVoice && it.status == "ready" }

    fun reallyCall() {
        val account = acc
        if (account == null) { toast(ctx, "Add and enable a SIP account first (Settings)"); return }
        if (backendSettings.url.isBlank()) { toast(ctx, "Set the backend URL in Settings"); return }
        if (!account.agentMode && leads.any { it.status == "DNC" && sameNumber(it.phone, number) }) { toast(ctx, "This number is on your Do-Not-Call list. Not dialing."); return }
        if (aiEnabled && currentVoice == null) { toast(ctx, "AI voice is ON but no ready voice is selected. Pick one or turn AI voice off."); return }
        calls.start(backend, account, if (account.agentMode) "" else number.trim(), if (aiEnabled) currentVoice?.id else null) { rec ->
            lastRecord = rec
            scope.launch { repo.addHistory(rec) }
        }
    }
    fun proceedAfterMic() {
        if (aiEnabled) {
            val v = currentVoice
            if (requireTest && v != null && v.id !in tested) needTest = true else confirmAi = true
        } else reallyCall()
    }
    val micPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted && wantCall) { wantCall = false; proceedAfterMic() } else if (!granted) toast(ctx, "Microphone permission is required for calls")
    }
    fun placeCall() {
        if (paused && acc?.agentMode != true) { toast(ctx, "You are PAUSED. Press Resume to dial."); return }
        if (acc?.agentMode != true && number.isBlank()) { toast(ctx, "Enter a destination number"); return }
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            wantCall = true; micPermission.launch(Manifest.permission.RECORD_AUDIO); return
        }
        proceedAfterMic()
    }
    fun dialLead(l: Lead) {
        if (l.status == "DNC") { toast(ctx, "Do-Not-Call: dialing is blocked for this lead."); return }
        selectedLeadId = l.id; number = l.phone; tab = "Dialer"
    }

    if (callUi.state != "idle") {
        val leadForCall = leads.firstOrNull { it.id == selectedLeadId } ?: leads.firstOrNull { sameNumber(it.phone, callUi.number) }
        CallScreen(calls, callUi, leadForCall, script) { disp, note, cbAt ->
            val rec = lastRecord
            scope.launch {
                if (rec != null) repo.updateHistory(rec.id, disp, note)
                if (leadForCall != null) repo.updateLeads { list ->
                    list.map { l -> if (l.id == leadForCall.id) l.copy(
                        status = if (disp.isBlank()) l.status else disp,
                        notes = if (note.isBlank()) l.notes else note,
                        attempts = l.attempts + 1, lastCallAt = System.currentTimeMillis(),
                        callbackAt = when { disp == "CALLBK" -> cbAt; disp.isBlank() -> l.callbackAt; else -> 0L }) else l }
                }
            }
            selectedLeadId = ""
            calls.reset()
        }
        return
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("AI Clone Dialer") }, colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surfaceVariant, titleContentColor = MaterialTheme.colorScheme.primary)) },
        bottomBar = {
            NavigationBar(containerColor = MaterialTheme.colorScheme.surfaceVariant) {
                listOf("Dialer", "Leads", "History", "Voice", "Settings").forEach { item ->
                    NavigationBarItem(selected = tab == item, onClick = { tab = item }, label = { Text(item, maxLines = 1) }, icon = {
                        Icon(when (item) {
                            "Dialer" -> Icons.Default.Dialpad; "Leads" -> Icons.Default.People; "History" -> Icons.Default.History
                            "Voice" -> Icons.Default.GraphicEq; else -> Icons.Default.Settings }, item)
                    })
                }
            }
        }
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).padding(12.dp)) {
            val dayStart = startOfToday()
            val today = history.filter { it.startedAt >= dayStart }
            AgentBar(paused, pauseCode, statusSince, acc, aiEnabled, today.size, today.sumOf { it.seconds },
                onPause = { code -> paused = true; pauseCode = code; statusSince = System.currentTimeMillis() },
                onResume = { paused = false; pauseCode = ""; statusSince = System.currentTimeMillis() })
            if (busy.isNotEmpty()) { LinearProgressIndicator(Modifier.fillMaxWidth()); Text(busy, style = MaterialTheme.typography.bodySmall) }
            when (tab) {
                "Dialer" -> DialerTab(
                    acc = acc, voices = readyVoices, currentVoice = currentVoice, aiEnabled = aiEnabled,
                    number = number, onNumber = { number = it },
                    lead = leads.firstOrNull { it.id == selectedLeadId },
                    onAiToggle = { scope.launch { repo.setAiEnabled(it) } },
                    onPickVoice = { scope.launch { repo.setDefaultVoice(it) } },
                    onTestVoice = { v -> testProfile = v },
                    onNextLead = {
                        val nowMs = System.currentTimeMillis()
                        val pool = leads.filter { it.id != selectedLeadId && it.status != "DNC" }
                        val next = pool.filter { it.callbackAt in 1..nowMs }.minByOrNull { it.callbackAt } ?: pool.firstOrNull { it.status == "NEW" }
                        if (next == null) toast(ctx, "No due call-backs or NEW leads") else { selectedLeadId = next.id; number = next.phone }
                    },
                    onClearLead = { selectedLeadId = ""; number = "" },
                    paused = paused,
                    onCall = { placeCall() })
                "Leads" -> LeadsTab(leads, onAdd = { editLead = null; showLeadDialog = true }, onImport = { showImport = true },
                    onEdit = { editLead = it; showLeadDialog = true }, onDial = { dialLead(it) },
                    onDelete = { l -> scope.launch { repo.updateLeads { list -> list.filter { it.id != l.id } } } })
                "History" -> HistoryTab(history, onClear = { scope.launch { repo.clearHistory() } }, onRedial = { number = it; tab = "Dialer" })
                "Voice" -> VoiceTab(profiles, defaultVoice, backend,
                    onAdd = { showAddVoice = true }, onRefresh = { refresh() },
                    onReview = { v -> guarded("Loading consent…", { backend.consentText() }, { t -> pending = Pending(v.id, v.ownerName, v.ownerRelation, t) }) },
                    onClone = { v -> guarded("Creating clone… (can take a minute)", { backend.clone(v.id); backend.profiles() }, { profiles = it }) },
                    onDefault = { v -> scope.launch { repo.setDefaultVoice(v.id) } },
                    onTest = { v -> testProfile = v },
                    onDelete = { v -> guarded("Deleting…", { backend.delete(v.id); backend.profiles() }, { profiles = it; if (defaultVoice == v.id) scope.launch { repo.setDefaultVoice("") } }) })
                else -> SettingsTab(repo, backendSettings, accounts, activeAccount, ctx,
                    onAddAccount = { editAccount = null; showAccountDialog = true },
                    onEditAccount = { editAccount = it; showAccountDialog = true },
                    onUseAccount = { scope.launch { repo.setActiveAccount(it.id) } },
                    onDeleteAccount = { scope.launch { repo.deleteAccount(it.id) } })
            }
        }
    }

    if (showAccountDialog) SipAccountDialog(editAccount, { showAccountDialog = false }, { a ->
        scope.launch { repo.saveAccount(a); if (activeAccount.isEmpty()) repo.setActiveAccount(a.id) }; showAccountDialog = false
    })
    if (showLeadDialog) LeadDialog(editLead, { showLeadDialog = false }) { l ->
        showLeadDialog = false
        scope.launch { repo.updateLeads { list -> if (list.any { it.id == l.id }) list.map { if (it.id == l.id) l else it } else list + l } }
    }
    if (showImport) ImportDialog({ showImport = false }) { text ->
        showImport = false
        val parsed = parseLeads(text)
        scope.launch {
            var added = 0
            repo.updateLeads { list ->
                val fresh = parsed.filter { n -> list.none { sameNumber(it.phone, n.phone) } }
                added = fresh.size
                list + fresh
            }
            toast(ctx, "Imported $added new lead(s)")
        }
    }
    if (showAddVoice) AddVoiceDialog({ showAddVoice = false }) { name, owner, relation, picked ->
        showAddVoice = false
        guarded("Uploading sample…", { backend.createProfile(name, owner, relation, picked.first, picked.second, picked.third) },
            { (id, text) -> pending = Pending(id, owner, relation, text); refresh() })
    }
    pending?.let { p ->
        ConsentDialog(p, onDismiss = { pending = null; refresh() }) { accept, ack, typed, note ->
            pending = null
            guarded(if (accept) "Saving consent and cloning…" else "Declining…", {
                val st = backend.respondConsent(p.id, accept, ack, typed, note)
                if (accept && st == "consented") backend.clone(p.id)
                st
            }, { st -> toast(ctx, if (st == "declined") "Declined — nothing was cloned and the sample was deleted." else "Voice clone ready"); refresh() })
        }
    }
    testProfile?.let { v -> TestVoiceDialog(v, backend, { tested = tested + v.id }) { testProfile = null } }
    if (needTest) AlertDialog(
        onDismissRequest = { needTest = false }, title = { Text("Check your cloned voice first?") },
        text = { Text("You haven't tested this voice in this session. Record one sentence and hear it in your cloned voice before the call starts.") },
        confirmButton = { TextButton({ needTest = false; testProfile = currentVoice }) { Text("Test now") } },
        dismissButton = { TextButton({ needTest = false; confirmAi = true }) { Text("Skip, call anyway") } })
    if (confirmAi) AlertDialog(
        onDismissRequest = { confirmAi = false }, title = { Text("Start with AI voice?") },
        text = { Text("The other person will hear an AI-generated voice. Tell them the voice is AI-generated where the law or your provider requires it. Do not impersonate anyone to deceive them.") },
        confirmButton = { TextButton({ confirmAi = false; reallyCall() }) { Text("I understand — start") } },
        dismissButton = { TextButton({ confirmAi = false }) { Text("Cancel") } })
}

// ------------------------------------------------------------------------------------------------ Dialer
@Composable
private fun DialerTab(
    acc: SipAccount?, voices: List<VoiceProfile>, currentVoice: VoiceProfile?, aiEnabled: Boolean,
    number: String, onNumber: (String) -> Unit, lead: Lead?,
    onAiToggle: (Boolean) -> Unit, onPickVoice: (String) -> Unit, onTestVoice: (VoiceProfile) -> Unit,
    onNextLead: () -> Unit, onClearLead: () -> Unit, paused: Boolean, onCall: () -> Unit
) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
            Column(Modifier.padding(12.dp)) {
                Text("SIP: ${acc?.label ?: "not set — add one in Settings"}${if (acc?.agentMode == true) "  ·  VICIdial agent phone" else ""}", style = MaterialTheme.typography.bodyMedium)
                Text("Voice: ${currentVoice?.name ?: "none"}  ·  AI voice ${if (aiEnabled) "ON" else "OFF"}",
                    style = MaterialTheme.typography.bodyMedium, color = if (aiEnabled) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        // ---- AI voice controls
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Use my cloned voice on this call", Modifier.weight(1f))
                    Switch(aiEnabled, onAiToggle)
                }
                var expanded by remember { mutableStateOf(false) }
                ExposedDropdownMenuBox(expanded, { expanded = it }) {
                    OutlinedTextField(currentVoice?.name ?: if (voices.isEmpty()) "No ready voice — add one in Voice tab" else "Choose voice", {}, readOnly = true,
                        label = { Text("Voice (tap to change)") }, modifier = Modifier.menuAnchor().fillMaxWidth())
                    ExposedDropdownMenu(expanded, { expanded = false }) {
                        voices.forEach { v -> DropdownMenuItem(text = { Text(v.name) }, onClick = { onPickVoice(v.id); expanded = false }) }
                    }
                }
                OutlinedButton({ currentVoice?.let(onTestVoice) }, enabled = currentVoice != null, modifier = Modifier.fillMaxWidth()) {
                    Icon(Icons.Default.Mic, null); Spacer(Modifier.width(6.dp)); Text("Test my cloned voice before calling")
                }
                Text("If AI voice is ON and conversion fails, the other party hears silence — never your real voice.", style = MaterialTheme.typography.bodySmall)
            }
        }
        if (acc?.agentMode == true) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("VICIdial agent session", style = MaterialTheme.typography.titleMedium)
                    Text("1. Press Start session.\n2. When it says Waiting, log in to the VICIdial agent screen: Phone Login = ${acc?.username}.\n3. Dial / take calls in VICIdial as usual. The customer hears your cloned voice.", style = MaterialTheme.typography.bodyMedium)
                    Button(onCall, Modifier.fillMaxWidth().height(52.dp)) { Icon(Icons.Default.Call, null); Spacer(Modifier.width(6.dp)); Text("Start VICIdial session") }
                }
            }
        } else {
            // ---- lead panel
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    if (lead != null) {
                        Text("Lead: ${lead.name}", style = MaterialTheme.typography.titleMedium)
                        Text("${lead.phone} · ${if (lead.status == "NEW") "NEW" else dispositionLabel(lead.status)} · attempts ${lead.attempts}", style = MaterialTheme.typography.bodySmall)
                        if (lead.notes.isNotBlank()) Text(lead.notes, style = MaterialTheme.typography.bodySmall)
                        if (lead.callbackAt > 0) Text("Call-back scheduled: ${when_(lead.callbackAt)}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.secondary)
                    } else Text("No lead selected — dial a number or pick one in Leads.", style = MaterialTheme.typography.bodySmall)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onNextLead) { Icon(Icons.Default.SkipNext, null); Spacer(Modifier.width(4.dp)); Text("Next lead") }
                        if (lead != null) TextButton(onClearLead) { Text("Clear") }
                    }
                }
            }
            OutlinedTextField(number, onNumber, label = { Text("Phone number / SIP URI") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone), modifier = Modifier.fillMaxWidth())
            listOf("1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#").chunked(3).forEach { row ->
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    row.forEach { k -> OutlinedButton({ onNumber(number + k) }, Modifier.weight(1f).height(52.dp)) { Text(k) } }
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onCall, Modifier.weight(1f).height(52.dp), enabled = !paused, colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2E7D32), contentColor = Color.White)) {
                    Icon(Icons.Default.Call, null); Spacer(Modifier.width(6.dp)); Text(if (paused) "Paused — resume to dial" else "Call")
                }
                OutlinedButton({ onNumber(number.dropLast(1)) }, Modifier.height(52.dp)) { Icon(Icons.Default.Backspace, "Delete") }
            }
        }
        Spacer(Modifier.height(8.dp))
    }
}

// ------------------------------------------------------------------------------------------------ Leads
@Composable
private fun LeadsTab(leads: List<Lead>, onAdd: () -> Unit, onImport: () -> Unit, onEdit: (Lead) -> Unit, onDial: (Lead) -> Unit, onDelete: (Lead) -> Unit) {
    var query by remember { mutableStateOf("") }
    var filter by remember { mutableStateOf("All") }
    val now = System.currentTimeMillis()
    val shown = leads.filter { l ->
        (query.isBlank() || l.name.contains(query, true) || l.phone.contains(query, true)) && when (filter) {
            "New" -> l.status == "NEW"
            "Callbacks" -> l.callbackAt > 0
            "Done" -> l.status != "NEW" && l.status != "DNC"
            "DNC" -> l.status == "DNC"
            else -> true
        }
    }.sortedWith(compareByDescending<Lead> { it.callbackAt in 1..now }.thenBy { if (it.callbackAt > 0) it.callbackAt else Long.MAX_VALUE })
    Column(Modifier.fillMaxSize()) {
        Text("Leads", style = MaterialTheme.typography.titleLarge)
        Text("New: ${leads.count { it.status == "NEW" }}  ·  Called: ${leads.count { it.status != "NEW" }}  ·  Call-backs due: ${leads.count { it.callbackAt in 1..now }}  ·  DNC: ${leads.count { it.status == "DNC" }}", style = MaterialTheme.typography.bodySmall)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onAdd) { Icon(Icons.Default.Add, null); Spacer(Modifier.width(4.dp)); Text("Add") }
            OutlinedButton(onImport) { Text("Import list") }
        }
        OutlinedTextField(query, { query = it }, label = { Text("Search name / number") }, singleLine = true,
            leadingIcon = { Icon(Icons.Default.Search, null) }, modifier = Modifier.fillMaxWidth().padding(top = 6.dp))
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            listOf("All", "New", "Callbacks", "Done", "DNC").forEach { f -> FilterChip(selected = filter == f, onClick = { filter = f }, label = { Text(f) }) }
        }
        if (leads.isEmpty()) Text("No leads yet. Add one, or import a list (name,phone per line).", Modifier.padding(top = 12.dp))
        else if (shown.isEmpty()) Text("No leads match.", Modifier.padding(top = 12.dp))
        LazyColumn { items(shown, key = { it.id }) { l ->
            Card(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(l.name, style = MaterialTheme.typography.titleMedium)
                        Text("${l.phone} · ${if (l.status == "NEW") "NEW" else dispositionLabel(l.status)}${if (l.attempts > 0) " · ${l.attempts}x" else ""}", style = MaterialTheme.typography.bodySmall,
                            color = if (l.status == "DNC") MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
                        if (l.callbackAt > 0) Text("Call-back ${when_(l.callbackAt)}${if (l.callbackAt <= now) "  (DUE)" else ""}", style = MaterialTheme.typography.bodySmall,
                            color = if (l.callbackAt <= now) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.primary)
                        if (l.notes.isNotBlank()) Text(l.notes, style = MaterialTheme.typography.bodySmall, maxLines = 2)
                    }
                    IconButton({ onDial(l) }, enabled = l.status != "DNC") { Icon(Icons.Default.Call, "Dial") }
                    IconButton({ onEdit(l) }) { Icon(Icons.Default.Edit, "Edit") }
                    IconButton({ onDelete(l) }) { Icon(Icons.Default.Delete, "Delete") }
                }
            }
        } }
    }
}

@Composable
private fun LeadDialog(existing: Lead?, onDismiss: () -> Unit, onSave: (Lead) -> Unit) {
    var name by remember { mutableStateOf(existing?.name ?: "") }
    var phone by remember { mutableStateOf(existing?.phone ?: "") }
    var notes by remember { mutableStateOf(existing?.notes ?: "") }
    var status by remember { mutableStateOf(existing?.status ?: "NEW") }
    var expanded by remember { mutableStateOf(false) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text(if (existing == null) "Add lead" else "Edit lead") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                OutlinedTextField(name, { name = it }, label = { Text("Name") }, singleLine = true)
                OutlinedTextField(phone, { phone = it }, label = { Text("Phone") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone))
                OutlinedTextField(notes, { notes = it }, label = { Text("Notes") })
                ExposedDropdownMenuBox(expanded, { expanded = it }) {
                    OutlinedTextField(if (status == "NEW") "NEW" else dispositionLabel(status), {}, readOnly = true, label = { Text("Status") }, modifier = Modifier.menuAnchor().fillMaxWidth())
                    ExposedDropdownMenu(expanded, { expanded = false }) {
                        DropdownMenuItem(text = { Text("NEW") }, onClick = { status = "NEW"; expanded = false })
                        DISPOSITIONS.forEach { (code, label) -> DropdownMenuItem(text = { Text(label) }, onClick = { status = code; expanded = false }) }
                    }
                }
            }
        },
        confirmButton = { TextButton({ if (phone.isNotBlank()) onSave((existing ?: Lead(name = "", phone = "")).copy(name = name.ifBlank { phone }, phone = phone.trim(), notes = notes, status = status, callbackAt = if (status == "CALLBK") (existing?.callbackAt ?: 0L) else 0L)) }) { Text("Save") } },
        dismissButton = { TextButton(onDismiss) { Text("Cancel") } })
}

@Composable
private fun ImportDialog(onDismiss: () -> Unit, onImport: (String) -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var text by remember { mutableStateOf("") }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) scope.launch {
            text = withContext(Dispatchers.IO) { runCatching { ctx.contentResolver.openInputStream(uri)?.bufferedReader()?.use { it.readText() } }.getOrNull().orEmpty() }
        }
    }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("Import leads") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("One lead per line: name,phone  (or just the phone number). Duplicates are skipped.", style = MaterialTheme.typography.bodySmall)
                OutlinedTextField(text, { text = it }, label = { Text("Paste list here") }, minLines = 5, maxLines = 8, modifier = Modifier.fillMaxWidth())
                OutlinedButton({ picker.launch(arrayOf("*/*")) }) { Text("Load from CSV / TXT file") }
            }
        },
        confirmButton = { TextButton({ if (text.isNotBlank()) onImport(text) }) { Text("Import") } },
        dismissButton = { TextButton(onDismiss) { Text("Cancel") } })
}

// ------------------------------------------------------------------------------------------------ History
@Composable
private fun HistoryTab(history: List<CallRecord>, onClear: () -> Unit, onRedial: (String) -> Unit) {
    Column(Modifier.fillMaxSize()) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Call log", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
            TextButton(onClear) { Text("Clear") }
        }
        val today = history.filter { it.startedAt >= startOfToday() }
        Card(Modifier.fillMaxWidth().padding(bottom = 6.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
            Column(Modifier.padding(12.dp)) {
                Text("Today: ${today.size} calls · talk ${fmtLong(today.sumOf { it.seconds })} · AI voice ${today.count { it.aiVoice }}", style = MaterialTheme.typography.titleSmall)
                val byDisp = DISPOSITIONS.mapNotNull { (c, l) -> today.count { it.disposition == c }.takeIf { it > 0 }?.let { "$l: $it" } }
                Text(if (byDisp.isEmpty()) "No dispositions yet today." else byDisp.joinToString("  ·  "), style = MaterialTheme.typography.bodySmall)
            }
        }
        LazyColumn { items(history, key = { it.id }) { h ->
            Card(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
                Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(h.number, style = MaterialTheme.typography.titleMedium)
                        Text("${SimpleDateFormat("dd MMM HH:mm", Locale.getDefault()).format(Date(h.startedAt))} · ${fmt(h.seconds)} · ${if (h.aiVoice) "AI voice" else "own voice"} · ${h.result}", style = MaterialTheme.typography.bodySmall)
                        if (h.disposition.isNotBlank()) Text("Disposition: ${dispositionLabel(h.disposition)}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.primary)
                        if (h.note.isNotBlank()) Text(h.note, style = MaterialTheme.typography.bodySmall)
                    }
                    IconButton({ onRedial(h.number) }) { Icon(Icons.Default.Call, "Redial") }
                }
            }
        } }
    }
}

// ------------------------------------------------------------------------------------------------ Voice
@Composable
private fun VoiceTab(
    profiles: List<VoiceProfile>, defaultVoice: String, backend: BackendClient,
    onAdd: () -> Unit, onRefresh: () -> Unit, onReview: (VoiceProfile) -> Unit, onClone: (VoiceProfile) -> Unit,
    onDefault: (VoiceProfile) -> Unit, onTest: (VoiceProfile) -> Unit, onDelete: (VoiceProfile) -> Unit
) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Voice Studio", style = MaterialTheme.typography.titleLarge)
        Text("Add a voice from an MP3 file or record it right here. Nothing is cloned until the voice owner accepts the consent screen. You can keep several voices and switch between them in the Dialer.", style = MaterialTheme.typography.bodySmall)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onAdd) { Icon(Icons.Default.Add, null); Spacer(Modifier.width(4.dp)); Text("Add voice") }
            OutlinedButton(onRefresh) { Text("Refresh") }
        }
        if (profiles.isEmpty()) Text("No voices yet.")
        profiles.forEach { v ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text(v.name + if (v.id == defaultVoice) "  ★ default" else "", style = MaterialTheme.typography.titleMedium)
                    Text("Owner: ${v.ownerName} · ${v.status}")
                    if (v.error.isNotBlank()) Text(v.error, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        when (v.status) {
                            "awaiting_consent" -> TextButton({ onReview(v) }) { Text("Review consent") }
                            "consented", "failed" -> TextButton({ onClone(v) }) { Text("Create clone") }
                            "ready" -> { TextButton({ onDefault(v) }) { Text("Set default") }; TextButton({ onTest(v) }) { Text("Test voice") } }
                        }
                        TextButton({ onDelete(v) }) { Text("Delete / revoke") }
                    }
                }
            }
        }
    }
}

@Composable
private fun TestVoiceDialog(p: VoiceProfile, backend: BackendClient, onTested: () -> Unit, onDismiss: () -> Unit) {
    val scope = rememberCoroutineScope()
    val micGate = rememberMicGate()
    val recorder = remember { VoiceRecorder() }
    var phase by remember { mutableStateOf("idle") }   // idle | recording | converting | playing | done
    var secs by remember { mutableStateOf(0) }
    var original by remember { mutableStateOf<ByteArray?>(null) }
    var converted by remember { mutableStateOf<ByteArray?>(null) }
    var error by remember { mutableStateOf("") }

    fun startTest() = micGate {
        scope.launch {
            error = ""; converted = null; secs = 0; phase = "recording"
            val pcm = try { recorder.record(12) { secs = it } } catch (e: Exception) { error = e.message ?: "Recording failed"; phase = "idle"; return@launch }
            original = pcm
            if (pcm.size < 32000) { error = "Too short — speak for 2–3 seconds."; phase = "idle"; return@launch }
            phase = "converting"
            try {
                val out = backend.preview(p.id, pcm)
                converted = out; onTested(); phase = "playing"
                VoiceTools.playPcm(out); phase = "done"
            } catch (e: Exception) { error = e.message ?: "Conversion failed"; phase = "idle" }
        }
    }

    AlertDialog(onDismissRequest = { recorder.stop(); onDismiss() }, title = { Text("Test: ${p.name}") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Say a sentence (2–12 s). You will hear it back in your cloned voice, exactly as the other person would on a call.", style = MaterialTheme.typography.bodySmall)
                when (phase) {
                    "recording" -> { Text("● Recording… ${secs}s", color = MaterialTheme.colorScheme.error); Button({ recorder.stop() }) { Icon(Icons.Default.Stop, null); Spacer(Modifier.width(4.dp)); Text("Stop") } }
                    "converting" -> { LinearProgressIndicator(Modifier.fillMaxWidth()); Text("Converting to your cloned voice…") }
                    "playing" -> Text("▶ Playing your cloned voice…")
                    else -> Button({ startTest() }) { Icon(Icons.Default.Mic, null); Spacer(Modifier.width(4.dp)); Text(if (phase == "done") "Record again" else "Record & test") }
                }
                if (phase == "done") {
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        OutlinedButton({ converted?.let { b -> scope.launch { VoiceTools.playPcm(b) } } }) { Icon(Icons.Default.PlayArrow, null); Text("Cloned voice") }
                        OutlinedButton({ original?.let { b -> scope.launch { VoiceTools.playPcm(b) } } }) { Icon(Icons.Default.PlayArrow, null); Text("My original") }
                    }
                }
                if (error.isNotBlank()) Text(error, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
        },
        confirmButton = { TextButton({ recorder.stop(); onDismiss() }) { Text("Close") } })
}

@Composable
private fun AddVoiceDialog(onDismiss: () -> Unit, onUpload: (String, String, String, Triple<String, ByteArray, String>) -> Unit) {
    val ctx = LocalContext.current
    val micGate = rememberMicGate()
    val recorder = remember { VoiceRecorder() }
    var name by remember { mutableStateOf("") }
    var owner by remember { mutableStateOf("") }
    var relation by remember { mutableStateOf("self") }
    var picked by remember { mutableStateOf<Triple<String, ByteArray, String>?>(null) }
    var recording by remember { mutableStateOf(false) }
    var secs by remember { mutableStateOf(0) }
    var recPcm by remember { mutableStateOf<ByteArray?>(null) }
    val scope = rememberCoroutineScope()
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) scope.launch { picked = withContext(Dispatchers.IO) { readPicked(ctx, uri) }; recPcm = null }
    }
    fun startRecording() = micGate {
        scope.launch {
            secs = 0; recording = true
            try {
                val pcm = recorder.record(120) { secs = it }
                recPcm = pcm
                picked = Triple("recording.wav", VoiceTools.wav(pcm), "audio/wav")
            } catch (e: Exception) { toast(ctx, e.message ?: "Recording failed") }
            recording = false
        }
    }
    AlertDialog(onDismissRequest = { recorder.stop(); onDismiss() }, title = { Text("Add voice") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(name, { name = it }, label = { Text("Profile name") }, singleLine = true)
                OutlinedTextField(owner, { owner = it }, label = { Text("Voice owner's full name") }, singleLine = true)
                var expanded by remember { mutableStateOf(false) }
                ExposedDropdownMenuBox(expanded, { expanded = it }) {
                    OutlinedTextField(if (relation == "self") "This is my own voice" else "Someone else (with their permission)", {}, readOnly = true,
                        label = { Text("Whose voice?") }, modifier = Modifier.menuAnchor().fillMaxWidth())
                    ExposedDropdownMenu(expanded, { expanded = false }) {
                        DropdownMenuItem(text = { Text("This is my own voice") }, onClick = { relation = "self"; expanded = false })
                        DropdownMenuItem(text = { Text("Someone else (with their permission)") }, onClick = { relation = "other_with_permission"; expanded = false })
                    }
                }
                Text("Option 1 — choose a file", style = MaterialTheme.typography.titleSmall)
                OutlinedButton({ picker.launch(arrayOf("audio/*")) }, enabled = !recording) {
                    Text(if (recPcm == null) (picked?.let { "${it.first} (${it.second.size / 1024} KB)" } ?: "Choose MP3 / WAV / M4A") else "Choose MP3 / WAV / M4A")
                }
                Text("Option 2 — record now", style = MaterialTheme.typography.titleSmall)
                Text("Read anything aloud for 30–60 seconds in a quiet room, in your natural voice. More clean speech = a better clone.", style = MaterialTheme.typography.bodySmall)
                if (recording) {
                    Text("● Recording… ${secs}s", color = MaterialTheme.colorScheme.error)
                    Button({ recorder.stop() }) { Icon(Icons.Default.Stop, null); Spacer(Modifier.width(4.dp)); Text("Stop") }
                } else {
                    OutlinedButton({ startRecording() }) { Icon(Icons.Default.Mic, null); Spacer(Modifier.width(4.dp)); Text(if (recPcm == null) "Record" else "Record again") }
                    recPcm?.let { pcm ->
                        Text("Recorded ${pcm.size / 32000}s", style = MaterialTheme.typography.bodySmall)
                        OutlinedButton({ scope.launch { VoiceTools.playPcm(pcm) } }) { Icon(Icons.Default.PlayArrow, null); Text("Play my recording") }
                    }
                }
            }
        },
        confirmButton = {
            TextButton({
                val p = picked
                if (recording) toast(ctx, "Stop the recording first")
                else if (p != null && p.second.size < 32000) toast(ctx, "Recording is too short")
                else if (name.isNotBlank() && owner.isNotBlank() && p != null) onUpload(name, owner, relation, p)
                else toast(ctx, "Fill in the names and choose or record a sample")
            }) { Text("Continue") }
        },
        dismissButton = { TextButton({ recorder.stop(); onDismiss() }) { Text("Cancel") } })
}

@Composable
private fun ConsentDialog(p: Pending, onDismiss: () -> Unit, onDecision: (Boolean, Boolean, String, String) -> Unit) {
    var ack by remember { mutableStateOf(false) }
    var typed by remember { mutableStateOf("") }
    var note by remember { mutableStateOf("") }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("Voice cloning consent") },
        text = {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                item { Text(p.text) }
                item { Row(verticalAlignment = Alignment.CenterVertically) { Checkbox(ack, { ack = it }); Text("I understand the other party must be told the voice is AI-generated where required.", Modifier.weight(1f)) } }
                item { OutlinedTextField(typed, { typed = it }, label = { Text("Type the voice owner's name: ${p.ownerName}") }, singleLine = true, modifier = Modifier.fillMaxWidth()) }
                if (p.relation != "self") item { OutlinedTextField(note, { note = it }, label = { Text("How/when did they give permission?") }, modifier = Modifier.fillMaxWidth()) }
            }
        },
        confirmButton = { TextButton({ onDecision(true, ack, typed, note) }, enabled = ack && typed.isNotBlank()) { Text("Accept & clone") } },
        dismissButton = { TextButton({ onDecision(false, false, "", "") }) { Text("Decline (delete)") } })
}

// ------------------------------------------------------------------------------------------------ Settings (backend + SIP accounts)
@Composable
private fun SettingsTab(
    repo: AppRepository, s: BackendSettings, accounts: List<SipAccount>, activeAccount: String, ctx: Context,
    onAddAccount: () -> Unit, onEditAccount: (SipAccount) -> Unit, onUseAccount: (SipAccount) -> Unit, onDeleteAccount: (SipAccount) -> Unit
) {
    val scope = rememberCoroutineScope()
    var url by remember(s) { mutableStateOf(s.url) }
    var key by remember(s) { mutableStateOf(s.apiKey) }
    var eleven by remember(s) { mutableStateOf(s.elevenKey) }
    var result by remember { mutableStateOf("") }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("SIP accounts", style = MaterialTheme.typography.titleLarge)
        Button(onAddAccount) { Icon(Icons.Default.Add, null); Spacer(Modifier.width(4.dp)); Text("Add SIP / VICIdial account") }
        accounts.forEach { a ->
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text(a.label + if (a.id == activeAccount) "  ✓ active" else "", style = MaterialTheme.typography.titleMedium)
                    Text("${a.username}@${a.server}:${a.port} (${a.transport})${if (a.agentMode) " · VICIdial agent phone" else ""}")
                    if (a.transport != "UDP") Text("Gateway supports UDP only for now", style = MaterialTheme.typography.bodySmall)
                    Row {
                        TextButton({ onUseAccount(a) }) { Text("Use") }
                        TextButton({ onEditAccount(a) }) { Text("Edit") }
                        TextButton({ onDeleteAccount(a) }) { Text("Delete") }
                    }
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        Text("Backend & ElevenLabs", style = MaterialTheme.typography.titleLarge)
        OutlinedTextField(url, { url = it }, label = { Text("Backend URL (https://… or http://ip:8000)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(key, { key = it }, label = { Text("API key") }, singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        OutlinedTextField(eleven, { eleven = it }, label = { Text("ElevenLabs API key (for voice clone)") }, singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button({ scope.launch { repo.saveBackend(BackendSettings(url, key, eleven)); toast(ctx, "Saved") } }) { Text("Save") }
            OutlinedButton({
                scope.launch {
                    repo.saveBackend(BackendSettings(url, key, eleven))
                    result = try { val h = BackendClient(url, key, eleven).health(); "OK · voice provider: ${h.optString("voice_provider", "none")}" } catch (e: Exception) { "Failed: ${e.message}" }
                }
            }) { Text("Test connection") }
        }
        if (result.isNotEmpty()) Text(result)
        Spacer(Modifier.height(8.dp))
        Text("Agent script (SOP)", style = MaterialTheme.typography.titleLarge)
        Text("Shown on the call screen. Tags: {name} {first} {phone}", style = MaterialTheme.typography.bodySmall)
        val savedScript by repo.script.collectAsState(DEFAULT_SCRIPT)
        var scriptText by remember(savedScript) { mutableStateOf(savedScript) }
        OutlinedTextField(scriptText, { scriptText = it }, minLines = 6, label = { Text("Script / SOP") }, modifier = Modifier.fillMaxWidth())
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button({ scope.launch { repo.setScript(scriptText); toast(ctx, "Script saved") } }) { Text("Save script") }
            OutlinedButton({ scriptText = DEFAULT_SCRIPT }) { Text("Reset") }
        }
        val reqTest by repo.requireTest.collectAsState(true)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Ask me to test my cloned voice before an AI-voice call", Modifier.weight(1f))
            Switch(reqTest, { scope.launch { repo.setRequireTest(it) } })
        }
        Text("The ElevenLabs key is sent only to YOUR backend (use HTTPS) and used for clone + live voice conversion. SIP calls and AI voice run on your backend (see ai-backend/). SIP passwords are stored in app preferences in this build.", style = MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(16.dp))
    }
}

// ------------------------------------------------------------------------------------------------ Call screen
@Composable
private fun CallScreen(calls: CallClient, ui: CallUi, lead: Lead?, script: String, onDisposition: (String, String, Long) -> Unit) {
    var showKeys by remember { mutableStateOf(false) }
    var disp by remember { mutableStateOf("") }
    var cbDelay by remember { mutableStateOf(CALLBACK_CHOICES[2].second) }
    var note by remember { mutableStateOf(lead?.notes.orEmpty()) }
    val banner = when (ui.state) { "connected" -> Color(0xFF2E7D32); "waiting" -> Color(0xFFEF6C00); "ended" -> Color(0xFF455A64); else -> Color(0xFF1565C0) }
    val status = when (ui.state) {
        "connected" -> "INCALL · ${fmt(ui.seconds)}"
        "ended" -> "DISPO · call ended: ${ui.detail}"
        "waiting" -> "Waiting for VICIdial call…"
        else -> ui.state.replaceFirstChar { it.uppercase() } + "…"
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        Box(Modifier.fillMaxWidth().background(banner).padding(20.dp)) {
            Column {
                Text(ui.number, style = MaterialTheme.typography.headlineMedium, color = Color.White)
                Text(status, color = Color.White)
                if (ui.state == "waiting" && ui.detail.isNotBlank()) Text(ui.detail, style = MaterialTheme.typography.bodySmall, color = Color.White)
                if (ui.aiVoice) Text("AI voice active — the other party hears your cloned voice", style = MaterialTheme.typography.bodySmall, color = Color.White)
            }
        }
        Column(Modifier.padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (lead != null) Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text("Lead: ${lead.name}", style = MaterialTheme.typography.titleMedium)
                    Text(lead.phone, style = MaterialTheme.typography.bodySmall)
                    if (lead.notes.isNotBlank()) Text(lead.notes, style = MaterialTheme.typography.bodySmall)
                }
            }
            if (script.isNotBlank() && ui.state != "ended") Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text("Script", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.primary)
                    Text(fillScript(script, lead, ui.number), style = MaterialTheme.typography.bodyMedium)
                }
            }
            if (ui.message.isNotBlank()) Text(ui.message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            OutlinedTextField(note, { note = it }, label = { Text("Call notes") }, modifier = Modifier.fillMaxWidth())
            if (ui.state == "ended") {
                Text("Disposition", style = MaterialTheme.typography.titleMedium, modifier = Modifier.fillMaxWidth())
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    DISPOSITIONS.forEach { (code, label) -> FilterChip(selected = disp == code, onClick = { disp = code }, label = { Text(label) }) }
                }
                if (disp == "CALLBK") {
                    Text("Call back", style = MaterialTheme.typography.titleSmall, modifier = Modifier.fillMaxWidth())
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        CALLBACK_CHOICES.forEach { (label, ms) -> FilterChip(selected = cbDelay == ms, onClick = { cbDelay = ms }, label = { Text(label) }) }
                    }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button({ onDisposition(disp, note, System.currentTimeMillis() + cbDelay) }, enabled = disp.isNotBlank()) { Text("Save & close") }
                    OutlinedButton({ onDisposition("", note, 0L) }) { Text("Skip") }
                }
            } else {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilledTonalButton({ calls.setMuted(!ui.muted) }) { Icon(if (ui.muted) Icons.Default.MicOff else Icons.Default.Mic, "Mute"); Spacer(Modifier.width(4.dp)); Text(if (ui.muted) "Unmute" else "Mute") }
                    FilledTonalButton({ calls.setSpeaker(!ui.speaker) }) { Icon(Icons.Default.VolumeUp, "Speaker"); Spacer(Modifier.width(4.dp)); Text(if (ui.speaker) "Earpiece" else "Speaker") }
                    FilledTonalButton({ showKeys = !showKeys }) { Icon(Icons.Default.Dialpad, "Keypad") }
                }
                if (showKeys) listOf("123", "456", "789", "*0#").forEach { row ->
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) { row.forEach { k -> OutlinedButton({ calls.dtmf(k) }) { Text(k.toString()) } } }
                }
                Button({ calls.hangup() }, colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error)) {
                    Icon(Icons.Default.CallEnd, "Hang up"); Spacer(Modifier.width(6.dp)); Text(if (ui.state == "waiting") "End session" else "Hang up")
                }
            }
        }
    }
}

// ------------------------------------------------------------------------------------------------ SIP account dialog
@Composable
private fun SipAccountDialog(existing: SipAccount?, onDismiss: () -> Unit, onSave: (SipAccount) -> Unit) {
    var label by remember { mutableStateOf(existing?.label ?: "My SIP") }
    var username by remember { mutableStateOf(existing?.username ?: "") }
    var password by remember { mutableStateOf(existing?.password ?: "") }
    var server by remember { mutableStateOf(existing?.server ?: "") }
    var domain by remember { mutableStateOf(existing?.domain ?: "") }
    var proxy by remember { mutableStateOf(existing?.proxy ?: "") }
    var authId by remember { mutableStateOf(existing?.authId ?: "") }
    var callerId by remember { mutableStateOf(existing?.callerId ?: "") }
    var displayName by remember { mutableStateOf(existing?.displayName ?: "") }
    var port by remember { mutableStateOf((existing?.port ?: 5060).toString()) }
    var transport by remember { mutableStateOf(existing?.transport ?: "UDP") }
    var enabled by remember { mutableStateOf(existing?.enabled ?: true) }
    var agent by remember { mutableStateOf(existing?.agentMode ?: false) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text(if (existing == null) "Add SIP account" else "Edit SIP account") },
        text = {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                item { Row(verticalAlignment = Alignment.CenterVertically) { Text("VICIdial / Asterisk agent phone", Modifier.weight(1f)); Switch(agent, { agent = it }) } }
                item { Text(if (agent) "Register as the agent's phone; VICIdial calls this extension and the app answers." else "Normal SIP account: you dial numbers from the Dialer tab.", style = MaterialTheme.typography.bodySmall) }
                item { SipField("Account name", label) { label = it } }
                item { SipField(if (agent) "Phone extension (Phone Login)" else "SIP username", username) { username = it } }
                item { OutlinedTextField(password, { password = it }, label = { Text(if (agent) "Phone password" else "SIP password") }, singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth()) }
                item { SipField(if (agent) "VICIdial server IP" else "SIP server / IP", server) { server = it } }
                item { SipField("Port", port) { port = it } }
                if (!agent) {
                    item { SipField("SIP domain (optional)", domain) { domain = it } }
                    item { SipField("Outbound proxy (optional)", proxy) { proxy = it } }
                    item { SipField("Authentication ID (optional)", authId) { authId = it } }
                    item { SipField("Caller ID (optional)", callerId) { callerId = it } }
                    item { SipField("Display name (optional)", displayName) { displayName = it } }
                    item {
                        var expanded by remember { mutableStateOf(false) }
                        ExposedDropdownMenuBox(expanded, { expanded = it }) {
                            OutlinedTextField(transport, {}, readOnly = true, label = { Text("Transport") }, modifier = Modifier.menuAnchor().fillMaxWidth())
                            ExposedDropdownMenu(expanded, { expanded = false }) {
                                listOf("UDP", "TCP", "TLS").forEach { DropdownMenuItem(text = { Text(it) }, onClick = { transport = it; expanded = false }) }
                            }
                        }
                    }
                }
                item { Row(verticalAlignment = Alignment.CenterVertically) { Text("Enabled", Modifier.weight(1f)); Switch(enabled, { enabled = it }) } }
            }
        },
        confirmButton = {
            TextButton({
                if (username.isNotBlank() && server.isNotBlank()) onSave(SipAccount(id = existing?.id ?: UUID.randomUUID().toString(), label = label, username = username,
                    password = password, server = server, domain = domain, port = port.toIntOrNull() ?: 5060, proxy = proxy, authId = authId,
                    callerId = callerId, displayName = displayName, transport = if (agent) "UDP" else transport, enabled = enabled, agentMode = agent))
            }) { Text("Save") }
        },
        dismissButton = { TextButton(onDismiss) { Text("Cancel") } })
}

@Composable
private fun SipField(label: String, value: String, onValue: (String) -> Unit) =
    OutlinedTextField(value, onValue, label = { Text(label) }, modifier = Modifier.fillMaxWidth(), singleLine = true)

// ------------------------------------------------------------------------------------------------ Agent status bar (VICIdial style)
@Composable
private fun AgentBar(
    paused: Boolean, pauseCode: String, since: Long, acc: SipAccount?, aiEnabled: Boolean, callsToday: Int, talkToday: Int,
    onPause: (String) -> Unit, onResume: () -> Unit
) {
    var now by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) { while (true) { delay(1000); now = System.currentTimeMillis() } }
    var menu by remember { mutableStateOf(false) }
    val chip = if (paused) Color(0xFFEF6C00) else Color(0xFF2E7D32)
    val pauseLabel = PAUSE_CODES.firstOrNull { it.first == pauseCode }?.second ?: pauseCode
    Card(Modifier.fillMaxWidth().padding(bottom = 8.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
        Row(Modifier.padding(horizontal = 12.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Box(Modifier.background(chip, RoundedCornerShape(6.dp)).padding(horizontal = 10.dp, vertical = 3.dp)) {
                        Text(if (paused) "PAUSED" else "READY", color = Color.White, style = MaterialTheme.typography.labelLarge)
                    }
                    Text(if (paused && pauseLabel.isNotBlank()) "$pauseLabel · ${fmtLong(((now - since) / 1000).toInt().coerceAtLeast(0))}" else fmtLong(((now - since) / 1000).toInt().coerceAtLeast(0)),
                        style = MaterialTheme.typography.bodyMedium)
                }
                Text("${acc?.label ?: "No SIP account"}  ·  AI voice ${if (aiEnabled) "ON" else "OFF"}  ·  Calls today $callsToday  ·  Talk ${fmtLong(talkToday)}",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (paused) {
                Button(onResume, colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2E7D32), contentColor = Color.White)) {
                    Icon(Icons.Default.PlayArrow, null); Spacer(Modifier.width(4.dp)); Text("Resume")
                }
            } else {
                Box {
                    OutlinedButton({ menu = true }) { Icon(Icons.Default.Pause, null); Spacer(Modifier.width(4.dp)); Text("Pause") }
                    DropdownMenu(menu, { menu = false }) {
                        PAUSE_CODES.forEach { (code, label) -> DropdownMenuItem(text = { Text(label) }, onClick = { menu = false; onPause(code) }) }
                    }
                }
            }
        }
    }
}
