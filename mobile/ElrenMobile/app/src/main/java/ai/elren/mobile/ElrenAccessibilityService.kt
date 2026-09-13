package ai.elren.mobile

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class ElrenAccessibilityService : AccessibilityService() {
    companion object {
        @Volatile var instance: ElrenAccessibilityService? = null
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var lastUiMotionAt = 0L
    private val adaptiveOverlap = mutableMapOf("down" to 0.45, "up" to 0.45)

    override fun onServiceConnected() { instance = this }
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
        var seen = 0
        fun scan(node: AccessibilityNodeInfo?, depth: Int): Boolean {
            if (node == null) return false
            // Treat an abnormal/deceptively deep hierarchy as sensitive. This
            // avoids a stack overflow and, more importantly, never converts a
            // traversal limit into permission to act on an uninspected screen.
            if (depth > 20 || seen++ >= 2_000) return true
            if (isSensitive(node)) return true
            for (index in 0 until node.childCount) {
                if (scan(node.getChild(index), depth + 1)) return true
            }
            return false
        }
        return scan(rootInActiveWindow, 0)
    }

    private fun executeOnMain(action: String, args: JSONObject): JSONObject = when (action) {
        "inspect" -> JSONObject().put("package", rootInActiveWindow?.packageName?.toString().orEmpty()).put("requires_user_authentication", requiresUserAuthentication()).put("tree", inspectTree())
        else -> {
            if (action !in setOf("back", "home", "recents") && requiresUserAuthentication())
                error("Authentication, verification code, or credential screen detected; user takeover is required")
            executeOrdinaryAction(action, args)
        }
    }

    private fun executeOrdinaryAction(action: String, args: JSONObject): JSONObject = when (action) {
        "tap" -> gesture(args.getDouble("x").toFloat(), args.getDouble("y").toFloat(), args.getDouble("x").toFloat(), args.getDouble("y").toFloat(), 80)
        "scroll" -> scrollPage(args)
        "swipe" -> gesture(args.getDouble("x").toFloat(), args.getDouble("y").toFloat(), args.getDouble("end_x").toFloat(), args.getDouble("end_y").toFloat(), args.optLong("duration_ms", 450))
        "type" -> setText(args.getString("text"))
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

    private fun setText(text: String): JSONObject {
        require(text.length <= 20_000) { "Text is too long" }
        val focused = rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?: findEditable(rootInActiveWindow)
            ?: error("No editable field is focused")
        val bundle = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) }
        return JSONObject().put("accepted", focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle))
    }

    private fun findEditable(root: AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        var seen = 0
        fun visit(node: AccessibilityNodeInfo?, depth: Int): AccessibilityNodeInfo? {
            if (node == null || depth > 20 || seen++ >= 2_000) return null
            if (node.isEditable) return node
            for (index in 0 until node.childCount) {
                visit(node.getChild(index), depth + 1)?.let { return it }
            }
            return null
        }
        return visit(root, 0)
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
                .put("sensitive", sensitive)
                .put("enabled", node.isEnabled)
                .put("children", children)
        }
        return walk(rootInActiveWindow, 0)
    }
}
