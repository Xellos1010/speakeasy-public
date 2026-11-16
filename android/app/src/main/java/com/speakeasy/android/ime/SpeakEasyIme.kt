package com.speakeasy.android.ime

import android.content.Intent
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.inputmethodservice.InputMethodService
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import com.speakeasy.android.Phase
import com.speakeasy.android.R
import com.speakeasy.android.SpeakEasy
import com.speakeasy.android.ui.MainActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

/**
 * "Dictation anywhere": a minimal keyboard whose mic key records, transcribes on the operator's
 * own server, and commits the text into whatever field has focus. Tap = toggle, hold = talk.
 */
class SpeakEasyIme : InputMethodService() {
    private lateinit var se: SpeakEasy
    private var scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val main = Handler(Looper.getMainLooper())
    private var status: TextView? = null
    private var meter: ProgressBar? = null
    private var mic: ImageButton? = null
    private var modeBtn: Button? = null

    override fun onCreate() {
        super.onCreate()
        se = SpeakEasy.get(this)
    }

    private val dark get() = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    override fun onCreateInputView(): View {
        val bg = if (dark) Color.parseColor("#0F1115") else Color.parseColor("#EDEFF4")
        val fg = if (dark) Color.parseColor("#E8EAF0") else Color.parseColor("#12151C")
        val keyBg = if (dark) Color.parseColor("#222632") else Color.WHITE
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setBackgroundColor(bg); setPadding(dp(8), dp(6), dp(8), dp(10))
        }
        status = TextView(this).apply {
            setTextColor(fg); textSize = 15f; gravity = Gravity.CENTER
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        meter = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply { max = 100; progress = 0 }
        root.addView(status, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        root.addView(meter, LinearLayout.LayoutParams(MATCH_PARENT, dp(6)).apply { setMargins(dp(24), dp(4), dp(24), dp(6)) })

        fun key(label: String, desc: String, onClick: () -> Unit) = Button(this).apply {
            text = label; contentDescription = desc; setTextColor(fg); textSize = 16f; isAllCaps = false
            background = GradientDrawable().apply { cornerRadius = dp(10).toFloat(); setColor(keyBg) }
            minHeight = dp(52); minimumHeight = dp(52)
            setOnClickListener { onClick() }
        }

        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        val switchKey = key("🌐", "Switch keyboard") { switchKeyboard() }.apply {
            setOnLongClickListener { picker(); true }
        }
        modeBtn = key(se.settings.mode, "Dictation mode, tap to change") { cycleMode() }
        mic = ImageButton(this).apply {
            setImageResource(R.drawable.ic_mic); contentDescription = "Dictate"
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(Color.parseColor("#2563EB")) }
            setColorFilter(Color.WHITE)
            setOnTouchListener(MicTouch())
        }
        val back = key("⌫", "Delete") { deleteOne() }
        val enter = key("⏎", "Enter") { enter() }
        val lp = { w: Float -> LinearLayout.LayoutParams(0, dp(56), w).apply { setMargins(dp(4), 0, dp(4), 0) } }
        row.addView(switchKey, lp(1f)); row.addView(modeBtn, lp(1.3f))
        row.addView(mic, LinearLayout.LayoutParams(dp(84), dp(84)).apply { setMargins(dp(8), 0, dp(8), 0) })
        row.addView(back, lp(1f)); row.addView(enter, lp(1f))
        root.addView(row, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        val space = key("space", "Space") { currentInputConnection?.commitText(" ", 1) }
        root.addView(space, LinearLayout.LayoutParams(MATCH_PARENT, dp(48)).apply { setMargins(dp(60), dp(8), dp(60), 0) })
        return root
    }

    override fun onStartInputView(info: EditorInfo?, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        scope.cancel(); scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
        modeBtn?.text = se.settings.mode
        setStatus(idleText())
        scope.launch { se.level.collectLatest { meter?.progress = (it * 100).toInt() } }
        scope.launch {
            se.phase.collectLatest { p ->
                mic?.setImageResource(if (p == Phase.Recording) R.drawable.ic_stop else R.drawable.ic_mic)
                (mic?.background as? GradientDrawable)?.setColor(Color.parseColor(when (p) {
                    Phase.Recording -> "#E5484D"; Phase.Sending -> "#6B7280"; Phase.Idle -> "#2563EB" }))
                mic?.contentDescription = if (p == Phase.Recording) "Stop and insert text" else "Dictate"
            }
        }
        if (!se.secure.paired) setStatus("Open SpeakEasy to pair with your Mac")
        else se.checkHealth()
    }

    override fun onFinishInputView(finishingInput: Boolean) {
        super.onFinishInputView(finishingInput)
        if (se.phase.value == Phase.Recording) se.cancelRecording()
        scope.cancel()
        se.flushEvents()
    }

    private fun idleText(): String {
        val q = se.queued.value
        return "Tap mic to talk · hold to talk · ${se.settings.mode}" + if (q > 0) " · $q queued" else ""
    }

    private fun setStatus(s: String) { main.post { status?.text = s } }

    private inner class MicTouch : View.OnTouchListener {
        private var downAt = 0L
        private var startedHere = false
        override fun onTouch(v: View, e: MotionEvent): Boolean {
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    v.isPressed = true
                    downAt = System.currentTimeMillis()
                    startedHere = false
                    when (se.phase.value) {
                        Phase.Idle -> { startedHere = startRec() }
                        else -> {}
                    }
                }
                MotionEvent.ACTION_UP -> {
                    v.isPressed = false
                    val held = System.currentTimeMillis() - downAt > 450
                    if (startedHere) { if (held) finish() }       // hold-to-talk release
                    else if (se.phase.value == Phase.Recording) finish() // second tap stops
                    v.performClick()
                }
                MotionEvent.ACTION_CANCEL -> { v.isPressed = false; if (startedHere) finish() }
            }
            return true
        }
    }

    private fun startRec(): Boolean {
        if (!se.hasMic()) {
            setStatus("Microphone permission needed — opening SpeakEasy")
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return false
        }
        val err = se.startRecording()
        return if (err != null) { setStatus(err); false } else { setStatus("Listening… tap or release to insert"); true }
    }

    private fun finish() {
        val pkg = currentInputEditorInfo?.packageName
        setStatus("Transcribing…")
        se.stopAndSend(se.settings.mode, appHint = pkg, source = "ime") { entry ->
            main.post {
                when (entry.status) {
                    "done" -> {
                        val ic = currentInputConnection
                        if (ic != null && entry.text.isNotBlank()) {
                            val before = ic.getTextBeforeCursor(1, 0)?.toString().orEmpty()
                            val sep = if (before.isNotEmpty() && !before.last().isWhitespace()) " " else ""
                            ic.commitText(sep + entry.text, 1)
                            setStatus("Inserted · ${entry.latencyMs ?: "?"} ms" + if (entry.rewritten) " · AI rewritten" else "")
                        } else setStatus(if (entry.text.isBlank()) "No speech detected" else "Field closed — text saved in SpeakEasy")
                    }
                    "queued" -> setStatus("Offline — saved, will transcribe later (see SpeakEasy app)")
                    else -> setStatus("Error: ${entry.error} — saved in SpeakEasy")
                }
            }
        }
    }

    private fun cycleMode() {
        val modes = SpeakEasy.MODES
        val next = modes[(modes.indexOf(se.settings.mode) + 1) % modes.size]
        se.settings.mode = next; modeBtn?.text = next
        se.logEvent("mode_choice", "mode" to next, "source" to "ime", "app_hint" to currentInputEditorInfo?.packageName)
        setStatus(idleText())
    }

    private fun deleteOne() {
        val ic = currentInputConnection ?: return
        val sel = ic.getSelectedText(0)
        if (!sel.isNullOrEmpty()) ic.commitText("", 1) else ic.deleteSurroundingText(1, 0)
    }

    private fun enter() {
        val ic = currentInputConnection ?: return
        val action = (currentInputEditorInfo?.imeOptions ?: 0) and EditorInfo.IME_MASK_ACTION
        if (action != EditorInfo.IME_ACTION_NONE && action != EditorInfo.IME_ACTION_UNSPECIFIED &&
            (currentInputEditorInfo?.imeOptions ?: 0) and EditorInfo.IME_FLAG_NO_ENTER_ACTION == 0) {
            ic.performEditorAction(action)
        } else {
            ic.sendKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_ENTER))
            ic.sendKeyEvent(KeyEvent(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_ENTER))
        }
    }

    private fun switchKeyboard() {
        val switched = if (Build.VERSION.SDK_INT >= 28) switchToPreviousInputMethod() else false
        if (!switched) picker()
    }

    private fun picker() { getSystemService(InputMethodManager::class.java).showInputMethodPicker() }

    override fun onDestroy() { scope.cancel(); super.onDestroy() }
}
