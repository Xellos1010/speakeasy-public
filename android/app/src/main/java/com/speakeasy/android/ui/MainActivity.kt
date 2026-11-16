package com.speakeasy.android.ui

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.speakeasy.android.SpeakEasy
import com.speakeasy.android.data.PairPayload
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private lateinit var se: SpeakEasy
    private var paired by mutableStateOf(false)
    private var pairPrefill by mutableStateOf<Pair<String?, String?>>(null to null)
    private var autoStart by mutableStateOf(false)

    private val micPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (!ok) se.setNotice("Microphone permission denied — SpeakEasy cannot record")
        else if (autoStart) { autoStart = false; se.startRecording()?.let { se.setNotice(it) } }
    }
    private val notifPerm = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }
    private var scanError by mutableStateOf<String?>(null)
    private val scanner = registerForActivityResult(ScanContract()) { r ->
        val p = PairPayload.parse(r.contents)
        if (r.contents == null) return@registerForActivityResult
        if (p == null) scanError = "That QR code is not a SpeakEasy pairing code"
        else { scanError = null; pairPrefill = p.code to p.url }
    }

    private fun scanQr() {
        scanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            .setPrompt("Scan the pairing QR shown by `speakeasy pair` on your Mac")
            .setBeepEnabled(false).setOrientationLocked(false))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        se = SpeakEasy.get(this)
        paired = se.secure.paired
        handleIntent(intent)
        setContent {
            SpeakEasyTheme {
                if (!paired) PairScreen(se, pairPrefill.first, pairPrefill.second, scanError, onScan = { scanQr() }) {
                    paired = true; askPerms()
                }
                else DictateScreen(se, requestMic = { askMic() }, onUnpair = { se.unpair(); paired = false })
            }
        }
        if (paired) askPerms()
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (isActive) { if (paired) se.checkHealth(); delay(15_000) }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(i: Intent?) {
        val data: Uri? = i?.data
        if (data != null && data.scheme == "speakeasy" && data.host == "pair") {
            // speakeasy://pair?code=123456&url=https://... (deep link alternative to the JSON QR)
            PairPayload.parse(data.toString())?.let { p ->
                pairPrefill = p.code to p.url
                if (paired) { se.unpair(); paired = false }
            }
        }
        if (i?.action == ACTION_DICTATE && se.secure.paired) {
            se.logEvent("launch_dictate", "source" to (i.getStringExtra("source") ?: "shortcut"))
            if (se.hasMic()) se.startRecording()?.let { se.setNotice(it) }
            else { autoStart = true; micPerm.launch(Manifest.permission.RECORD_AUDIO) }
        }
    }

    private fun askMic() { micPerm.launch(Manifest.permission.RECORD_AUDIO) }

    private fun askPerms() {
        if (!se.hasMic()) askMic()
        else if (Build.VERSION.SDK_INT >= 33) notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    override fun onStop() {
        super.onStop()
        se.flushEvents()
    }

    companion object { const val ACTION_DICTATE = "com.speakeasy.android.DICTATE" }
}
