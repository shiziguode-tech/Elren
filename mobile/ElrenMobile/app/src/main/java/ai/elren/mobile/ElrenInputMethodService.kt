package ai.elren.mobile

import android.app.KeyguardManager
import android.content.ComponentName
import android.content.Context
import android.provider.Settings
import android.inputmethodservice.InputMethodService
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.ExtractedTextRequest
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit

/** User-selected IME. No exported command receiver: only the paired control service calls it. */
class ElrenInputMethodService : InputMethodService() {
    companion object {
        @Volatile var instance: ElrenInputMethodService? = null
            private set
        fun isSelected(context: Context): Boolean {
            val value = Settings.Secure.getString(context.contentResolver, Settings.Secure.DEFAULT_INPUT_METHOD)
            return ComponentName.unflattenFromString(value.orEmpty()) == ComponentName(context, ElrenInputMethodService::class.java)
        }
    }
    private val main = Handler(Looper.getMainLooper())
    private var generation = 0L
    private var active = false
    fun readyFor(packageName: String): Boolean = active && currentInputConnection != null &&
        currentInputEditorInfo?.packageName == packageName
    override fun onCreate() { super.onCreate(); instance = this }
    override fun onDestroy() { if (instance === this) instance = null; super.onDestroy() }
    override fun onStartInput(attribute: EditorInfo?, restarting: Boolean) {
        super.onStartInput(attribute, restarting)
        generation++
        active = attribute != null
    }
    override fun onFinishInput() { active = false; generation++; super.onFinishInput() }
    override fun onCreateInputView(): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(24, 16, 24, 16)
        addView(TextView(context).apply {
            text = "Elren 辅助输入 · 仅接收已配对电脑的输入操作\n需要手动打字时，请切回常用键盘。"
            textSize = 16f
        })
        addView(Button(context).apply {
            text = "切换键盘"
            setOnClickListener { getSystemService(InputMethodManager::class.java).showInputMethodPicker() }
        })
    }

    private fun <T> onMain(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        val pending = FutureTask<T> { block() }
        main.post(pending)
        try { return pending.get(4, TimeUnit.SECONDS) }
        catch (error: java.util.concurrent.ExecutionException) { throw error.cause ?: error }
        finally { if (!pending.isDone) pending.cancel(false) }
    }

    fun replaceVerified(text: String, accessibility: ElrenAccessibilityService): JSONObject {
        check(Looper.myLooper() != Looper.getMainLooper())
        require(text.length <= 20_000)
        val pinned = onMain {
            check(active) { "Elren keyboard is selected but no editor is active; tap the intended input field." }
            Triple(generation, currentInputEditorInfo, currentInputConnection)
        }
        val editor = pinned.second ?: error("No active editor; no input was attempted")
        val connection = pinned.third ?: error("No active input connection; no input was attempted")
        fun validate() {
            check(isSelected(this) && active && generation == pinned.first && currentInputEditorInfo === editor) {
                "Input focus changed; no further input is allowed"
            }
            check(!getSystemService(KeyguardManager::class.java).isDeviceLocked &&
                !accessibility.requiresUserAuthentication()) { "Protected screen; user action is required" }
            check(accessibility.rootInActiveWindow?.packageName?.toString() == editor.packageName) {
                "Foreground application differs from editor; no input is allowed"
            }
            val cls = editor.inputType and InputType.TYPE_MASK_CLASS
            val variation = editor.inputType and InputType.TYPE_MASK_VARIATION
            val password = cls == InputType.TYPE_CLASS_TEXT && variation in setOf(
                InputType.TYPE_TEXT_VARIATION_PASSWORD, InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD,
                InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD) ||
                cls == InputType.TYPE_CLASS_NUMBER && variation == InputType.TYPE_NUMBER_VARIATION_PASSWORD
            val marker = "${editor.hintText?.toString().orEmpty()} ${editor.fieldName.orEmpty()}".lowercase()
            check(!password && !Regex("otp|verification.?code|passcode|password|验证码|校验码|动态码|口令").containsMatchIn(marker)) {
                "Protected input field; user action is required"
            }
        }
        val adapter = object : VerifiedEditorReplacement.Connection {
            override fun snapshot(): VerifiedEditorReplacement.Snapshot? = onMain {
                validate()
                // Legacy editors often implement extracted text while the newer
                // accessibility getSurroundingText API returns null.
                val extracted = connection.getExtractedText(ExtractedTextRequest().apply {
                    hintMaxChars = 20_001
                    hintMaxLines = 1_000
                }, 0) ?: return@onMain null
                EditorSnapshotValidation.complete(extracted.text?.toString(), extracted.startOffset,
                    extracted.partialStartOffset, extracted.partialEndOffset,
                    extracted.selectionStart, extracted.selectionEnd)
            }
            override fun selectAll(length: Int) = onMain {
                validate()
                check(connection.setSelection(0, length)) { "Editor rejected selection; no input was committed" }
            }
            override fun commit(text: String) = onMain {
                validate()
                check(connection.commitText(text, 1)) { "Editor rejected text; inspect before retrying" }
            }
        }
        val target = object : VerifiedTextInput.Target {
            override fun read(): String = adapter.snapshot()?.text ?: error(
                "IME_READ_UNAVAILABLE: native editor did not provide a complete draft. No blind replacement is allowed; this does not prove text input is impossible.")
            // Native IME has no accessibility node; use its verified selection/commit transaction.
            override fun setText(text: String) = Unit
            override fun replaceThroughInputConnection(text: String, expected: String): Boolean {
                val guarded = object : VerifiedEditorReplacement.Connection by adapter {
                    override fun commit(text: String) {
                        val latest = adapter.snapshot()
                        check(latest != null && latest.text == expected && latest.start == 0 && latest.end == expected.length) {
                            "Draft or selection changed; no text was committed"
                        }
                        adapter.commit(text)
                    }
                }
                return VerifiedEditorReplacement().replace(guarded, text, expected)
            }
        }
        VerifiedTextInput().replace(target, text)
        return JSONObject().put("accepted", true).put("verified", true)
            .put("input_method", "elren_native_ime").put("characters", text.length)
    }
}

internal object EditorSnapshotValidation {
    @Suppress("UNUSED_PARAMETER") // partialEnd is meaningful only for partial updates.
    fun complete(text: String?, offset: Int, partialStart: Int, partialEnd: Int,
                 start: Int, end: Int): VerifiedEditorReplacement.Snapshot? {
        if (text == null || text.length > 20_000 || offset != 0 || partialStart != -1 ||
            start !in 0..text.length || end !in 0..text.length) return null
        return VerifiedEditorReplacement.Snapshot(text, offset, start, end)
    }
}
