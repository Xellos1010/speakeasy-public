@file:OptIn(ExperimentalMaterial3Api::class)

package com.speakeasy.android.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.widget.Toast
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.onClick
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.speakeasy.android.Conn
import com.speakeasy.android.Phase
import com.speakeasy.android.R
import com.speakeasy.android.SpeakEasy
import com.speakeasy.android.data.HistoryEntry
import java.text.DateFormat
import java.util.Date

@Composable
fun DictateScreen(se: SpeakEasy, requestMic: () -> Unit, onUnpair: () -> Unit) {
    val conn by se.conn.collectAsState()
    val phase by se.phase.collectAsState()
    val level by se.level.collectAsState()
    val queued by se.queued.collectAsState()
    val history by se.history.collectAsState()
    val current by se.current.collectAsState()
    val notice by se.notice.collectAsState()
    var mode by remember { mutableStateOf(se.settings.mode) }
    var editing by remember { mutableStateOf<HistoryEntry?>(null) }
    var menu by remember { mutableStateOf(false) }
    val ctx = LocalContext.current

    fun start() {
        if (!se.hasMic()) { requestMic(); return }
        se.startRecording()?.let { se.setNotice(it) }
    }
    fun stop() = se.stopAndSend(mode, source = "app")

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        LazyColumn(
            Modifier.safeDrawingPadding().padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("SpeakEasy", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold,
                        modifier = Modifier.weight(1f))
                    StatusPill(conn, queued, Modifier.clickable { se.checkHealth() })
                    TextButton(onClick = { menu = !menu }, modifier = Modifier.heightIn(min = 48.dp)) { Text("⋯") }
                }
                if (menu) Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = { menu = false; se.checkHealth() }) { Text("Retry now") }
                    TextButton(onClick = { menu = false; onUnpair() }) { Text("Unpair") }
                }
            }
            item {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                    SpeakEasy.MODES.forEach { m ->
                        FilterChip(
                            selected = mode == m,
                            onClick = { mode = m; se.settings.mode = m; se.logEvent("mode_choice", "mode" to m, "source" to "app") },
                            label = { Text(m.replaceFirstChar { it.uppercase() }, style = MaterialTheme.typography.titleSmall) },
                            modifier = Modifier.heightIn(min = 44.dp).semantics { contentDescription = "Mode $m" },
                        )
                    }
                }
            }
            item {
                Column(Modifier.fillMaxWidth().padding(vertical = 8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                    MicButton(phase, level, onPressStart = { start() }, onRelease = { held ->
                        // hold-to-talk: release after a hold stops; a quick tap leaves it recording (toggle)
                        if (held) stop()
                    }, onTapWhileRecording = { stop() })
                    Spacer(Modifier.height(12.dp))
                    LinearProgressIndicator(
                        progress = { if (phase == Phase.Recording) level else 0f },
                        modifier = Modifier.fillMaxWidth(0.7f).height(8.dp).clip(RoundedCornerShape(4.dp))
                            .semantics { contentDescription = "Input level" },
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        when (phase) {
                            Phase.Idle -> "Tap to talk · hold to talk"
                            Phase.Recording -> "Listening… tap to send (or release)"
                            Phase.Sending -> "Transcribing…"
                        },
                        style = MaterialTheme.typography.titleMedium,
                        modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                    )
                    notice?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyLarge) }
                }
            }
            current?.let { e -> item { ResultCard(e, ctx, onEdit = { editing = e }, se = se) } }
            if (history.size > 1) item {
                Text("History", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            }
            items(history.filter { it.id != current?.id }, key = { it.id }) { e ->
                HistoryRow(e) { se.select(e) }
            }
            item { Spacer(Modifier.height(24.dp)) }
        }
    }

    editing?.let { e ->
        var text by remember(e.id) { mutableStateOf(e.text) }
        AlertDialog(
            onDismissRequest = { editing = null },
            title = { Text("Correct transcript") },
            text = {
                OutlinedTextField(text, { text = it }, modifier = Modifier.fillMaxWidth().heightIn(min = 140.dp),
                    textStyle = MaterialTheme.typography.bodyLarge)
            },
            confirmButton = {
                TextButton(onClick = {
                    editing = null
                    se.sendFeedback(e, text) { err ->
                        if (err != null) se.setNotice("Correction saved locally; server: $err")
                    }
                }) { Text("Save & teach") }
            },
            dismissButton = { TextButton(onClick = { editing = null }) { Text("Cancel") } },
        )
    }
}

@Composable
fun StatusPill(conn: Conn, queued: Int, modifier: Modifier = Modifier) {
    val (color, text) = when (conn) {
        is Conn.Connected -> Foundry.Ok to (if (queued > 0) "Connected · $queued queued" else "Connected")
        is Conn.Offline -> Foundry.Warn to (if (queued > 0) "Offline · $queued queued" else "Offline")
        is Conn.Error -> Foundry.Err to "Error"
        Conn.NotPaired -> Foundry.Warn to "Not paired"
        Conn.Unknown -> MaterialTheme.colorScheme.outline to "Checking…"
    }
    val reason = (conn as? Conn.Offline)?.reason ?: (conn as? Conn.Error)?.reason
    Column(modifier.semantics(mergeDescendants = true) {
        contentDescription = "Server status: $text" + (reason?.let { ". $it" } ?: "")
    }) {
        Row(
            Modifier.clip(RoundedCornerShape(50)).background(color.copy(alpha = 0.16f))
                .padding(horizontal = 12.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(Modifier.size(10.dp).clip(CircleShape).background(color))
            Spacer(Modifier.width(8.dp))
            Text(text, style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurface)
        }
        reason?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2, overflow = TextOverflow.Ellipsis, modifier = Modifier.width(220.dp).padding(top = 2.dp))
        }
    }
}

@Composable
private fun MicButton(phase: Phase, level: Float, onPressStart: () -> Unit, onRelease: (held: Boolean) -> Unit,
                     onTapWhileRecording: () -> Unit) {
    val recording = phase == Phase.Recording
    val curPhase = rememberUpdatedState(phase)
    val curStart = rememberUpdatedState(onPressStart)
    val curRelease = rememberUpdatedState(onRelease)
    val curTap = rememberUpdatedState(onTapWhileRecording)
    val scale by animateFloatAsState(if (recording) 1f + level * 0.18f else 1f, label = "mic")
    val bg = when (phase) {
        Phase.Recording -> Foundry.Err
        Phase.Sending -> MaterialTheme.colorScheme.outline
        Phase.Idle -> MaterialTheme.colorScheme.primary
    }
    Box(
        Modifier.size(168.dp).scale(scale).clip(CircleShape).background(bg)
            .semantics {
                role = Role.Button
                contentDescription = if (recording) "Stop and transcribe" else "Start dictation"
                stateDescription = phase.name
                onClick { if (recording) onTapWhileRecording() else onPressStart(); true }
            }
            // Keyed on Unit: starting a recording changes `phase`, and re-keying would cancel the
            // in-flight press so a hold-to-talk release would never be seen.
            .pointerInput(Unit) {
                detectTapGestures(onPress = {
                    val p0 = curPhase.value
                    if (p0 == Phase.Sending) return@detectTapGestures
                    if (p0 == Phase.Recording) {
                        tryAwaitRelease(); curTap.value(); return@detectTapGestures
                    }
                    val t0 = System.currentTimeMillis()
                    curStart.value()
                    tryAwaitRelease()
                    curRelease.value(System.currentTimeMillis() - t0 > 450)
                })
            },
        contentAlignment = Alignment.Center,
    ) {
        Icon(painterResource(if (recording) R.drawable.ic_stop else R.drawable.ic_mic), contentDescription = null,
            tint = Color.White, modifier = Modifier.size(72.dp))
    }
}

@Composable
private fun ResultCard(e: HistoryEntry, ctx: Context, onEdit: () -> Unit, se: SpeakEasy) {
    var showRaw by remember(e.id) { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth(), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(e.mode.uppercase(), style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (e.rewritten) {
                    Spacer(Modifier.width(8.dp))
                    Text("AI rewritten", style = MaterialTheme.typography.labelLarge, color = Foundry.Ai,
                        modifier = Modifier.clip(RoundedCornerShape(6.dp)).background(Foundry.Ai.copy(alpha = 0.15f))
                            .padding(horizontal = 6.dp, vertical = 2.dp))
                }
                if (e.edited) { Spacer(Modifier.width(8.dp)); Text("Edited", style = MaterialTheme.typography.labelLarge) }
                Spacer(Modifier.weight(1f))
                e.latencyMs?.let { Text("${it} ms", style = MaterialTheme.typography.labelMedium) }
            }
            when (e.status) {
                "queued" -> Text("Saved on phone — will transcribe when the server is reachable.\n${e.error ?: ""}",
                    color = Foundry.Warn, style = MaterialTheme.typography.bodyLarge)
                "error" -> Text("Saved on phone. Server said: ${e.error}", color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodyLarge)
                else -> Text(if (showRaw) e.rawText else e.text.ifEmpty { "(no speech detected)" },
                    style = MaterialTheme.typography.bodyLarge.copy(lineHeight = MaterialTheme.typography.bodyLarge.lineHeight))
            }
            if (e.status == "done") Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                TextButton(onClick = {
                    val cm = ctx.getSystemService(ClipboardManager::class.java)
                    cm.setPrimaryClip(ClipData.newPlainText("SpeakEasy", e.text))
                    se.logEvent("copy", "utterance_id" to e.utteranceId, "mode" to e.mode)
                    Toast.makeText(ctx, "Copied", Toast.LENGTH_SHORT).show()
                }, modifier = Modifier.heightIn(min = 48.dp)) { Text("Copy") }
                TextButton(onClick = {
                    se.logEvent("share", "utterance_id" to e.utteranceId, "mode" to e.mode)
                    ctx.startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                        type = "text/plain"; putExtra(Intent.EXTRA_TEXT, e.text)
                    }, "Share dictation"))
                }, modifier = Modifier.heightIn(min = 48.dp)) { Text("Share") }
                TextButton(onClick = onEdit, modifier = Modifier.heightIn(min = 48.dp)) { Text("Edit") }
                if (e.rewritten) TextButton(onClick = { showRaw = !showRaw }, modifier = Modifier.heightIn(min = 48.dp)) {
                    Text(if (showRaw) "AI" else "Raw")
                }
            }
        }
    }
}

@Composable
private fun HistoryRow(e: HistoryEntry, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().heightIn(min = 56.dp).clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface).clickable(onClick = onClick).padding(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(
                when (e.status) { "queued" -> "⏳ Queued"; "error" -> "⚠ ${e.error}"; else -> e.text.ifEmpty { "(empty)" } },
                maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodyLarge,
            )
            Text("${e.mode}${if (e.rewritten) " · AI" else ""} · " +
                DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(e.createdAt)),
                style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
