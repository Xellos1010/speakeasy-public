package com.speakeasy.android.ui

import android.os.Build
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.speakeasy.android.Conn
import com.speakeasy.android.SpeakEasy
import kotlinx.coroutines.launch

@Composable
fun PairScreen(se: SpeakEasy, prefillCode: String?, prefillUrl: String?, scanError: String?,
               onScan: () -> Unit, onPaired: () -> Unit) {
    var url by remember(prefillUrl) { mutableStateOf(prefillUrl ?: se.settings.serverUrl) }
    var code by remember(prefillCode) { mutableStateOf(prefillCode ?: "") }
    var name by remember { mutableStateOf(se.settings.deviceName ?: "${Build.MANUFACTURER} ${Build.MODEL}") }
    var health by remember { mutableStateOf<Conn>(Conn.Unknown) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(url) { health = Conn.Unknown; health = se.probe(url) }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(
            Modifier.safeDrawingPadding().padding(20.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Text("SpeakEasy", style = MaterialTheme.typography.headlineLarge, fontWeight = FontWeight.Bold)
            Text("Pair this phone with your Mac. Audio goes only to your own server over your tailnet.",
                style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
            StatusPill(health, queued = 0)
            Button(onClick = onScan, modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)
                .semantics { contentDescription = "Scan pairing QR code from the Mac" }) {
                Text("Scan pairing QR", style = MaterialTheme.typography.titleMedium)
            }
            scanError?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyLarge) }
            Text("or enter the code", style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            OutlinedTextField(url, { url = it.trim() }, label = { Text("Server URL") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                modifier = Modifier.fillMaxWidth())
            OutlinedTextField(code, { v -> code = v.filter { it.isDigit() }.take(6) },
                label = { Text("6-digit pairing code") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword),
                textStyle = MaterialTheme.typography.headlineSmall,
                modifier = Modifier.fillMaxWidth().semantics { contentDescription = "Pairing code, 6 digits" })
            OutlinedTextField(name, { name = it }, label = { Text("Device name") }, singleLine = true,
                modifier = Modifier.fillMaxWidth())
            error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodyLarge) }
            Button(
                onClick = {
                    busy = true; error = null
                    scope.launch {
                        val err = se.pair(url, code, name)
                        busy = false
                        if (err == null) onPaired() else error = "Pairing failed: $err"
                    }
                },
                enabled = !busy && code.length == 6 && url.startsWith("http"),
                modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp),
            ) { Text(if (busy) "Pairing…" else "Pair", style = MaterialTheme.typography.titleMedium) }
            OutlinedButton(onClick = { scope.launch { health = Conn.Unknown; health = se.probe(url) } },
                modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("Check server") }
            Text("On the Mac run: speakeasy pair --device note10 — it prints a 6-digit code (valid 10 min) " +
                "and a QR code. Scan the QR or type the code.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
