package ai.elren.mobile

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.accessibilityservice.GestureDescription
import android.accessibilityservice.InputMethod
import android.annotation.TargetApi
import android.app.KeyguardManager
import android.text.InputType
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.net.Uri
import android.os.Bundle
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Callable
import java.util.concurrent.ExecutionException
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit

class ElrenAccessibilityService : AccessibilityService() {
    companion object {
        @Volatile var instance: ElrenAccessibilityService? = null
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var lastUiMotionAt = 0L
    private val adaptiveOverlap = mutableMapOf("down" to 0.45, "up" to 0.45)
    private val textInputLock = Any()

    override fun onServiceConnected() {
        if (Build.VERSION.SDK_INT >= 33) {
            // Use Android's accessibility input connection; no keyboard switch or clipboard needed.
            serviceInfo = serviceInfo.apply {
                flags = flags or AccessibilityServiceInfo.FLAG_INPUT_METHOD_EDITOR
            }
        }
        instance = this
    }
    override fun onUnbind(intent: Intent?): Boolean {
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }
    override fun onDestroy() { if (instance === this) instance = null; super.onDestroy() }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType in setOf(
                AccessibilityEvent.TYPE_VIEW_SCROLLED,
                AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED,
                AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            )
        ) {
            lastUiMotionAt = SystemClock.elapsedRealtime()
        }
    }
    override fun onInterrupt() = Unit

    fun execute(action: String, args: JSONObject): JSONObject {
        if (action == "type") return synchronized(textInputLock) { setTextVerified(args.getString("text")) }
        val latch = CountDownLatch(1)
        var output: JSONObject = JSONObject().put("accepted", false)
        var failure: Throwable? = null
        main.post {
            try { output = executeOnMain(action, args) } catch (error: Throwable) { failure = error } finally { latch.countDown() }
        }
        if (!latch.await(8, TimeUnit.SECONDS)) error("Phone action timed out")
        failure?.let { throw it }
        return output
    }

    fun requiresUserAuthentication(): Boolean {
        val result = AuthenticationScreenScan.scan(rootInActiveWindow,
            children = { node -> (0 until node.childCount.coerceAtMost(10_001)).mapNotNull(node::getChild) },
            protected = { node -> AuthenticationScreenScan.isAuthenticationField(
                node.isVisibleToUser, node.isPassword, node.isEditable, isSensitive(node)) })
        check(result != AuthenticationScreenScan.Result.UNKNOWN) {
            "UI_INSPECTION_INCOMPLETE: unable to verify active UI; this does NOT establish a locked or authentication screen. Refresh accessibility state and inspect again."
        }
        return result == AuthenticationScreenScan.Result.AUTHENTICATION
    }

    private fun executeOnMain(action: String, args: JSONObject): JSONObject = when (action) {
        "inspect" -> JSONObject().put("package", rootInActiveWindow?.packageName?.toString().orEmpty())
            .put("requires_user_authentication", requiresUserAuthentication()).put("tree", inspectTree())
            .put("app_build", BuildConfig.VERSION_CODE)
            .put("elren_keyboard_selected", ElrenInputMethodService.isSelected(this))
            .put("elren_keyboard_connected", ElrenInputMethodService.instance != null)
        else -> {
            if (action !in setOf("back", "home", "recents") && requiresUserAuthentication())
                error("AUTHENTICATION_FIELD_VISIBLE: visible credential input detected; this does NOT establish device lock. Ask the user to handle that field only; do not instruct screen unlock.")
            executeOrdinaryAction(action, args)
        }
    }

    private fun executeOrdinaryAction(action: String, args: JSONObject): JSONObject = when (action) {
        "tap" -> gesture(args.getDouble("x").toFloat(), args.getDouble("y").toFloat(), args.getDouble("x").toFloat(), args.getDouble("y").toFloat(), 80)
        "scroll" -> scrollPage(args)
        "swipe" -> gesture(args.getDouble("x").toFloat(), args.getDouble("y").toFloat(), args.getDouble("end_x").toFloat(), args.getDouble("end_y").toFloat(), args.optLong("duration_ms", 450))
        "back" -> global(GLOBAL_ACTION_BACK)
        "home" -> global(GLOBAL_ACTION_HOME)
        "recents" -> global(GLOBAL_ACTION_RECENTS)
        "launch" -> launch(args.getString("package"))
        "open_url" -> openUrl(args.getString("url"))
        "visible_content" -> JSONObject().put("items", visibleContent())
        else -> error("Unsupported accessibility action: $action")
    }

    fun waitForUiStability(minimumWaitMs: Long): JSONObject {
        val started = SystemClock.elapsedRealtime()
        val minimum = minimumWaitMs.coerceIn(250, 5_500)
        val deadline = started + minimum + 3_000
        var settled = false
        while (SystemClock.elapsedRealtime() < deadline) {
            Thread.sleep(80)
            val now = SystemClock.elapsedRealtime()
            val minimumElapsed = now - started >= minimum
            val quietFor = now - maxOf(lastUiMotionAt, started)
            if (minimumElapsed && quietFor >= 420) {
                settled = true
                break
            }
        }
        return JSONObject()
            .put("settled", settled)
            .put("waited_ms", SystemClock.elapsedRealtime() - started)
            .put("quiet_window_ms", 420)
    }

    @Synchronized fun updateAdaptiveOverlap(
        direction: String,
        requested: Double,
        observed: Double,
        newItemCount: Int,
    ): Double {
        val next = when {
            newItemCount == 0 -> requested - 0.08
            observed < 0.18 -> requested + 0.08
            observed > 0.62 -> requested - 0.07
            else -> requested
        }.coerceIn(0.28, 0.68)
        adaptiveOverlap[direction] = next
        return next
    }

    private fun scrollPage(args: JSONObject): JSONObject {
        val direction = args.optString("direction", "down").lowercase()
        require(direction in setOf("down", "up")) { "Scroll direction must be down or up" }
        val overlap = if (args.has("overlap_ratio")) {
            args.optDouble("overlap_ratio", 0.45)
        } else {
            synchronized(this) { adaptiveOverlap[direction] ?: 0.45 }
        }.coerceIn(0.25, 0.75)
        val container = largestScrollable(rootInActiveWindow) ?: rootInActiveWindow
            ?: error("No active Android window is available")
        val bounds = Rect().also(container::getBoundsInScreen)
        require(bounds.width() > 40 && bounds.height() > 160) { "No usable scroll area is visible" }
        val margin = (bounds.height() * 0.12f).coerceAtLeast(36f)
        val available = (bounds.height() - margin * 2).coerceAtLeast(120f)
        val distance = (bounds.height() * (1.0 - overlap)).toFloat()
            .coerceIn(available * 0.30f, available * 0.72f)
        val centerX = bounds.exactCenterX()
        val startY: Float
        val endY: Float
        if (direction == "down") {
            startY = bounds.bottom - margin
            endY = (startY - distance).coerceAtLeast(bounds.top + margin)
        } else {
            startY = bounds.top + margin
            endY = (startY + distance).coerceAtMost(bounds.bottom - margin)
        }
        val duration = args.optLong("duration_ms", 900).coerceIn(650, 1_600)
        val before = visibleContent()
        lastUiMotionAt = SystemClock.elapsedRealtime()
        return gesture(centerX, startY, centerX, endY, duration)
            .put("direction", direction)
            .put("overlap_target", overlap)
            .put("movement_fraction", kotlin.math.abs(endY - startY) / bounds.height())
            .put("duration_ms", duration)
            .put("scroll_bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
            .put("before_visible_items", before)
    }

    private fun largestScrollable(root: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        var best: AccessibilityNodeInfo? = null
        var bestArea = 0L
        fun visit(node: AccessibilityNodeInfo?) {
            if (node == null) return
            if (node.isScrollable && node.isVisibleToUser) {
                val bounds = Rect().also(node::getBoundsInScreen)
                val area = bounds.width().toLong() * bounds.height().toLong()
                if (area > bestArea) {
                    best = node
                    bestArea = area
                }
            }
            for (index in 0 until node.childCount) visit(node.getChild(index))
        }
        visit(root)
        return best
    }

    private fun visibleContent(): JSONArray {
        val values = linkedSetOf<String>()
        fun visit(node: AccessibilityNodeInfo?, depth: Int) {
            if (node == null || depth > 12 || values.size >= 100) return
            if (node.isVisibleToUser && !isSensitive(node)) {
                val text = node.text?.toString()?.trim().orEmpty()
                val description = node.contentDescription?.toString()?.trim().orEmpty()
                val value = when {
                    text.isNotEmpty() -> text
                    description.isNotEmpty() -> description
                    else -> ""
                }
                if (value.isNotEmpty()) values.add(value.take(300))
            }
            for (index in 0 until node.childCount) visit(node.getChild(index), depth + 1)
        }
        visit(rootInActiveWindow, 0)
        return JSONArray(values.toList())
    }

    private fun isSensitive(node: AccessibilityNodeInfo): Boolean {
        if (node.isPassword) return true
        val marker = listOf(node.viewIdResourceName, node.hintText, node.contentDescription)
            .joinToString(" ") { it?.toString().orEmpty() }
            .lowercase()
        return Regex("(^|[^a-z])(otp|one.?time|verification.?code|auth.?code|passcode|password|验证码|校验码|动态码|口令)([^a-z]|$)").containsMatchIn(marker)
    }

    private fun global(code: Int) = JSONObject().put("accepted", performGlobalAction(code))

    private fun launch(packageName: String): JSONObject {
        require(packageName.matches(Regex("[A-Za-z0-9_.]{3,240}"))) { "Invalid Android package name" }
        val intent = packageManager.getLaunchIntentForPackage(packageName) ?: error("App is not installed: $packageName")
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        startActivity(intent)
        return JSONObject().put("accepted", true).put("package", packageName)
    }

    private fun openUrl(value: String): JSONObject {
        val uri = Uri.parse(value)
        require(uri.scheme in setOf("https", "http")) { "Only HTTP(S) URLs can be opened" }
        startActivity(Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        return JSONObject().put("accepted", true).put("url", value)
    }

    private fun <T> onMain(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        val task = FutureTask(Callable(block))
        main.post(task)
        try {
            return task.get(3, TimeUnit.SECONDS)
        } catch (error: ExecutionException) {
            throw (error.cause ?: error)
        } finally {
            // Do not leave a queued input running after a timeout.
            task.cancel(false)
            main.removeCallbacks(task)
        }
    }

    private fun usableTextField(node: AccessibilityNodeInfo): Boolean {
        val bounds = Rect().also(node::getBoundsInScreen)
        return node.isEditable && node.isEnabled && node.isVisibleToUser &&
            !isSensitive(node) && !bounds.isEmpty
    }

    private fun chooseTextField(): AccessibilityNodeInfo? {
        check(!requiresUserAuthentication()) { "Protected screen detected; user takeover is required" }
        val roots = windows.filter {
            it.type == AccessibilityWindowInfo.TYPE_APPLICATION && (it.isActive || it.isFocused)
        }.mapNotNull { it.root }.ifEmpty { listOfNotNull(rootInActiveWindow) }
        val fields = mutableListOf<AccessibilityNodeInfo>()
        var seen = 0
        fun visit(node: AccessibilityNodeInfo?, depth: Int) {
            if (node == null) return
            check(depth <= 20 && seen++ < 2_000) { "Input hierarchy is too large; tap the intended field and retry" }
            if (usableTextField(node) && fields.none { it == node }) fields.add(node)
            for (index in 0 until node.childCount) visit(node.getChild(index), depth + 1)
        }
        roots.forEach { visit(it, 0) }
        val focused = fields.filter { it.isFocused }
        val field = when {
            focused.size == 1 -> focused.single()
            focused.isEmpty() && fields.size == 1 -> fields.single()
            fields.isEmpty() -> return null
            else -> error("Multiple editable fields; tap the intended input field before typing")
        }
        if (!field.isFocused) {
            field.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
            field.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        }
        return field
    }

    private fun requireSameTextField(node: AccessibilityNodeInfo) {
        check(!requiresUserAuthentication() && node.refresh() && usableTextField(node)) {
            "Input field disappeared or became protected; inspect the screen before retrying"
        }
        check(node.isFocused && findFocus(AccessibilityNodeInfo.FOCUS_INPUT) == node) {
            "Input focus changed; tap the intended field and retry"
        }
    }

    @TargetApi(33)
    private fun matchingInputConnection(node: AccessibilityNodeInfo): InputMethod.AccessibilityInputConnection? {
        val method = inputMethod ?: return null
        val editor = method.currentInputEditorInfo ?: return null
        if (editor.packageName != node.packageName?.toString()) return null
        // Reject a stale connection to a different editor in the same application.
        if (editor.fieldId != 0 && !node.viewIdResourceName.isNullOrEmpty()) {
            val name = runCatching {
                packageManager.getResourcesForApplication(editor.packageName).getResourceName(editor.fieldId)
            }.getOrNull() ?: return null
            if (name != node.viewIdResourceName) return null
        }
        return method.currentInputConnection
    }

    private val keyboardTransaction = java.util.concurrent.locks.ReentrantLock()

    private fun setTextVerified(text: String): JSONObject {
        require(text.length <= 20_000) { "Text is too long" }
        check(Looper.myLooper() != Looper.getMainLooper()) { "Text verification must run on a worker" }
        check(keyboardTransaction.tryLock()) { "Another keyboard transaction is active" }
        try {
            val target = onMain {
                getSystemService(android.view.inputmethod.InputMethodManager::class.java)
                    .enabledInputMethodList.firstOrNull {
                        it.packageName == packageName && it.serviceName == ElrenInputMethodService::class.java.name
                    }?.id
            }
            if (Build.VERSION.SDK_INT >= 30 && target != null) return withAutomaticKeyboard(target, text)
            return setTextWithExistingMethod(text)
        } finally { keyboardTransaction.unlock() }
    }

    @TargetApi(30)
    private fun withAutomaticKeyboard(target: String, text: String): JSONObject {
        val foreground = onMain {
            check(!requiresUserAuthentication()) { "Protected screen; user action required" }
            chooseTextField() // Focus a visible editor when available; never invent coordinates.
            rootInActiveWindow?.packageName?.toString() ?: error("No foreground app; no input attempted")
        }
        val port = object : TemporaryInputMethod.Port {
            override fun selected(): String = onMain {
                android.provider.Settings.Secure.getString(contentResolver,
                    android.provider.Settings.Secure.DEFAULT_INPUT_METHOD).orEmpty()
            }
            override fun switchTo(id: String): Boolean = onMain { softKeyboardController.switchToInputMethod(id) }
            override fun ready(): Boolean = onMain {
                check(!requiresUserAuthentication() && rootInActiveWindow?.packageName?.toString() == foreground) {
                    "Foreground/protected state changed; input cancelled"
                }
                ElrenInputMethodService.instance?.readyFor(foreground) == true
            }
            override fun pause() { Thread.sleep(50) }
        }
        return TemporaryInputMethod(port).use(target) {
            val keyboard = ElrenInputMethodService.instance ?: error("Keyboard disconnected; no input attempted")
            keyboard.replaceVerified(text, this)
        }
    }

    private fun setTextWithExistingMethod(text: String): JSONObject {
        if (ElrenInputMethodService.isSelected(this)) {
            val keyboard = ElrenInputMethodService.instance
                ?: error("Elren keyboard is selected but not connected yet; tap the input field once, then retry")
            return keyboard.replaceVerified(text, this)
        }
        val field = onMain { chooseTextField() }
        if (field == null) {
            check(Build.VERSION.SDK_INT >= 33) { "No visible editable field; tap the intended input field first" }
            return setTextThroughFocusedEditor(text)
        }
        // Let focus and the input connection settle without blocking accessibility callbacks.
        Thread.sleep(250)
        val target = object : VerifiedTextInput.Target {
            override fun read(): String = onMain {
                requireSameTextField(field)
                val nodeText = if (field.isShowingHintText) "" else field.text?.toString().orEmpty()
                if (Build.VERSION.SDK_INT >= 33) {
                    val surrounding = matchingInputConnection(field)?.getSurroundingText(20_001, 20_001, 0)
                    if (surrounding != null && surrounding.offset == 0 && surrounding.text.length <= 20_000) {
                        // Prefer the actual editor over a transient accessibility SET_TEXT echo.
                        return@onMain surrounding.text.toString()
                    }
                }
                nodeText
            }
            override fun setText(text: String) = onMain {
                requireSameTextField(field)
                val bundle = Bundle().apply {
                    putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
                }
                field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle)
                Unit // Acceptance is not verification; the transaction reads the field back.
            }
            override fun replaceThroughInputConnection(text: String, expected: String): Boolean {
                if (Build.VERSION.SDK_INT < 33) return false
                return replaceWithEditor(field, text, expected)
            }
        }
        val method = VerifiedTextInput().replace(target, text)
        return JSONObject().put("accepted", true).put("verified", true)
            .put("input_method", method).put("characters", text.length)
    }

    @TargetApi(33)
    private fun setTextThroughFocusedEditor(text: String): JSONObject {
        // Some apps (including WeChat tablet layouts) hide the entire editable accessibility tree.
        // The platform's already-focused editor remains the authoritative input destination.
        val editor = onMain { inputMethod?.currentInputEditorInfo }
            ?: error("No active text input connection; tap the intended field to activate input")
        fun validate() {
            check(!getSystemService(KeyguardManager::class.java).isDeviceLocked && !requiresUserAuthentication()) {
                "Protected screen detected; user takeover is required"
            }
            check(inputMethod?.currentInputEditorInfo === editor &&
                rootInActiveWindow?.packageName?.toString() == editor.packageName) {
                "Active editor changed; inspect the screen before retrying"
            }
            val inputClass = editor.inputType and InputType.TYPE_MASK_CLASS
            val variation = editor.inputType and InputType.TYPE_MASK_VARIATION
            check(!(inputClass == InputType.TYPE_CLASS_TEXT && variation in setOf(
                InputType.TYPE_TEXT_VARIATION_PASSWORD, InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD,
                InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD,
            )) && !(inputClass == InputType.TYPE_CLASS_NUMBER && variation == InputType.TYPE_NUMBER_VARIATION_PASSWORD)) {
                "Protected input field; user takeover is required"
            }
            val marker = "${editor.hintText?.toString().orEmpty()} ${editor.fieldName.orEmpty()}".lowercase()
            check(!Regex("otp|verification.?code|passcode|password|验证码|校验码|动态码|口令").containsMatchIn(marker)) {
                "Protected input field; user takeover is required"
            }
        }
        val connection = onMain { validate(); inputMethod?.currentInputConnection }
            ?: error("No active text input connection; tap the intended field first")
        var writeAttempted = false
        val adapter = object : VerifiedEditorReplacement.Connection {
            override fun snapshot(): VerifiedEditorReplacement.Snapshot? = onMain {
                validate()
                connection.getSurroundingText(20_001, 20_001, 0)?.let {
                    if (it.offset != 0 || it.text.length > 20_000) return@let null
                    VerifiedEditorReplacement.Snapshot(it.text.toString(), it.offset, it.selectionStart, it.selectionEnd)
                }
            }
            override fun selectAll(length: Int) = onMain {
                validate()
                connection.setSelection(0, length)
            }
            override fun commit(text: String) = onMain {
                validate()
                writeAttempted = true
                connection.commitText(text, 1, null)
            }
        }
        var expectedAtCommit = ""
        val target = object : VerifiedTextInput.Target {
            override fun read(): String = adapter.snapshot()?.text
                ?: error(if (writeAttempted)
                    "INPUT_VERIFICATION_UNAVAILABLE: a write was attempted but could not be verified. Inspect the field before retrying to avoid duplicate text."
                else
                    "INPUT_READ_UNAVAILABLE: accessibility could not read the complete editor, so NO WRITE was attempted. This does NOT prove WeChat cannot accept text. Ask the user to enable and select Elren 辅助输入 in the phone Elren app, then retry once. Do not infer previous message delivery from this error.")
            override fun setText(text: String) = Unit // No exposed node; proceed to the verified editor route.
            override fun replaceThroughInputConnection(text: String, expected: String): Boolean {
                expectedAtCommit = expected
                val guarded = object : VerifiedEditorReplacement.Connection by adapter {
                    override fun commit(text: String) {
                        val latest = adapter.snapshot()
                        check(latest != null && latest.text == expectedAtCommit && latest.start == 0 && latest.end == expectedAtCommit.length) {
                            "Input selection changed; no text was committed"
                        }
                        adapter.commit(text)
                    }
                }
                return VerifiedEditorReplacement().replace(guarded, text, expected)
            }
        }
        val method = VerifiedTextInput().replace(target, text)
        return JSONObject().put("accepted", true).put("verified", true)
            .put("input_method", method).put("characters", text.length)
            .put("target_source", "focused_editor")
    }

    @TargetApi(33)
    private fun replaceWithEditor(field: AccessibilityNodeInfo, text: String, expected: String): Boolean {
        // Pin the connection, but validate the live editor identity before each use.
        val connection = onMain {
            requireSameTextField(field)
            matchingInputConnection(field)
        } ?: return false
        val editor = object : VerifiedEditorReplacement.Connection {
            override fun snapshot(): VerifiedEditorReplacement.Snapshot? = onMain {
                requireSameTextField(field)
                if (matchingInputConnection(field) == null) return@onMain null
                val value = connection.getSurroundingText(20_001, 20_001, 0) ?: return@onMain null
                val nodeText = if (field.isShowingHintText) "" else field.text?.toString().orEmpty()
                // A stale SET_TEXT echo is allowed, but never replace a truncated or unrelated draft.
                if (nodeText != expected && nodeText != text) return@onMain null
                VerifiedEditorReplacement.Snapshot(
                    value.text.toString(), value.offset, value.selectionStart, value.selectionEnd,
                )
            }
            override fun selectAll(length: Int) = onMain {
                requireSameTextField(field)
                connection.setSelection(0, length)
            }
            override fun commit(text: String) = onMain {
                requireSameTextField(field)
                check(matchingInputConnection(field) != null) { "Input connection changed; retry after inspecting the screen" }
                val value = connection.getSurroundingText(20_001, 20_001, 0)
                check(value != null && value.offset == 0 && value.text.toString() == expected &&
                    value.selectionStart == 0 && value.selectionEnd == expected.length) {
                    "Input selection changed; no text was committed"
                }
                connection.commitText(text, 1, null)
            }
        }
        return VerifiedEditorReplacement().replace(editor, text, expected)
    }

    private fun gesture(startX: Float, startY: Float, endX: Float, endY: Float, duration: Long): JSONObject {
        val path = Path().apply { moveTo(startX, startY); lineTo(endX, endY) }
        val stroke = GestureDescription.StrokeDescription(path, 0, duration.coerceIn(50, 5000))
        val accepted = dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
        if (accepted) lastUiMotionAt = SystemClock.elapsedRealtime()
        return JSONObject().put("accepted", accepted)
    }

    private fun inspectTree(): JSONObject? {
        var seen = 0
        fun walk(node: AccessibilityNodeInfo?, depth: Int): JSONObject? {
            if (node == null || depth > 10 || seen++ >= 600) return null
            val bounds = Rect().also(node::getBoundsInScreen)
            val children = JSONArray()
            for (index in 0 until node.childCount) walk(node.getChild(index), depth + 1)?.let(children::put)
            val sensitive = isSensitive(node)
            return JSONObject()
                .put("class", node.className?.toString().orEmpty())
                .put("text", if (sensitive) "[protected]" else node.text?.toString().orEmpty().take(1000))
                .put("description", if (sensitive) "[protected]" else node.contentDescription?.toString().orEmpty().take(1000))
                .put("view_id", node.viewIdResourceName.orEmpty())
                .put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
                .put("clickable", node.isClickable)
                .put("editable", node.isEditable)
                .put("focused", node.isFocused)
                .put("visible", node.isVisibleToUser)
                .put("sensitive", sensitive)
                .put("enabled", node.isEnabled)
                .put("children", children)
        }
        return walk(rootInActiveWindow, 0)
    }
}
