package ai.elren.mobile

import android.Manifest
import android.app.Activity
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Color
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.CompoundButton
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.content.ContextCompat
import androidx.core.os.LocaleListCompat
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.google.android.material.dialog.MaterialAlertDialogBuilder

class MainActivity : AppCompatActivity() {
    private lateinit var store: SecureStore
    private lateinit var status: TextView
    private lateinit var screenCaptureState: TextView
    private lateinit var accessibilityButton: Button
    private lateinit var accessibilityState: TextView
    private lateinit var connectButton: Button
    private val statusHandler = Handler(Looper.getMainLooper())
    private var pairingInProgress = false
    private var unpairingInProgress = false
    private var projectionReceiverRegistered = false
    private var projectionRequestPending = false
    private var projectionRequestStartedAt = 0L

    private val projectionStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != CompanionForegroundService.ACTION_PROJECTION_STATE_CHANGED) return
            projectionRequestPending = false
            updateScreenCaptureState()
        }
    }

    private val statusRefresh = object : Runnable {
        override fun run() {
            updatePairingState()
            updateScreenCaptureState()
            statusHandler.postDelayed(this, 750)
        }
    }

    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        val code = result.contents
        if (code.isNullOrBlank()) {
            updatePairingState()
        } else {
            // Run after CaptureActivity has fully returned. This keeps the main
            // activity visible on Android/OEM builds that finish the scanner
            // transaction asynchronously.
            window.decorView.post { pairFromCode(code) }
        }
    }

    private val projectionLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode == Activity.RESULT_OK && result.data != null) {
            projectionRequestPending = true
            projectionRequestStartedAt = SystemClock.elapsedRealtime()
            val requestId = ProjectionGrantBroker.stage(result.resultCode, result.data!!)
            store.setScreenCaptureState(false, phase = "grant_received", ownerId = requestId)
            try {
                val service = Intent(this, CompanionForegroundService::class.java)
                    .setAction(CompanionForegroundService.ACTION_SET_PROJECTION)
                    .putExtra(CompanionForegroundService.EXTRA_PROJECTION_REQUEST_ID, requestId)
                ContextCompat.startForegroundService(this, service)
                screenCaptureState.text = text("● 正在启动本次屏幕读取…", "● Starting screen capture for this session…")
                screenCaptureState.setTextColor(Color.rgb(75, 85, 99))
            } catch (error: Throwable) {
                ProjectionGrantBroker.discard(requestId)
                projectionRequestPending = false
                store.setScreenCaptureState(
                    false,
                    "${error.javaClass.simpleName}: ${error.message}",
                    "error",
                )
                screenCaptureState.text = text(
                    "● 无法启动屏幕读取：${error.message}",
                    "● Screen capture could not start: ${error.message}",
                )
                screenCaptureState.setTextColor(Color.rgb(185, 28, 28))
            }
        } else {
            projectionRequestPending = false
            store.setScreenCaptureState(false, phase = "idle")
            updateScreenCaptureState()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = SecureStore(this)
        applyStoredLanguage()
        setContentView(buildContent())
        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 33)
        }
        consumePairingIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        consumePairingIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        if (::accessibilityButton.isInitialized) updateAccessibilityState()
        statusHandler.removeCallbacks(statusRefresh)
        statusHandler.post(statusRefresh)
        // Android 12+ permits foreground-service starts while transitioning
        // from a user-visible Activity. Waiting until after onResume avoids an
        // OEM race where onStart is still classified as background.
        statusHandler.postDelayed({
            if (!pairingInProgress && !isFinishing && !isDestroyed) connectIfPaired()
        }, 300)
    }

    override fun onStart() {
        super.onStart()
        if (!projectionReceiverRegistered) {
            ContextCompat.registerReceiver(
                this,
                projectionStateReceiver,
                IntentFilter(CompanionForegroundService.ACTION_PROJECTION_STATE_CHANGED),
                ContextCompat.RECEIVER_NOT_EXPORTED,
            )
            projectionReceiverRegistered = true
        }
        updateScreenCaptureState()
    }

    override fun onStop() {
        if (projectionReceiverRegistered) {
            unregisterReceiver(projectionStateReceiver)
            projectionReceiverRegistered = false
        }
        super.onStop()
    }

    override fun onPause() {
        statusHandler.removeCallbacks(statusRefresh)
        super.onPause()
    }

    private fun consumePairingIntent(intent: Intent?) {
        val value = intent?.dataString ?: return
        intent.data = null
        window.decorView.post { pairFromCode(value) }
    }

    private fun text(zh: String, en: String): String =
        if (store.usesChinese(resources.configuration.locales[0].language)) zh else en

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun buildContent(): ScrollView {
        val scroll = ScrollView(this).apply {
            isFillViewport = true
            clipToPadding = false
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(38), dp(24), dp(52))
            setBackgroundColor(Color.rgb(246, 248, 252))
        }
        fun title(value: String, size: Float, color: Int = Color.rgb(17, 24, 39)) = TextView(this).apply {
            text = value
            textSize = size
            setTextColor(color)
            setPadding(0, 0, 0, dp(10))
        }

        val heading = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        heading.addView(title("Elren", 28f), LinearLayout.LayoutParams(
            0,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            1f,
        ))
        heading.addView(Button(this).apply {
            text = when (store.interfaceLanguage()) {
                "zh" -> "中文"
                "en" -> "English"
                else -> text("跟随系统", "System language")
            }
            contentDescription = text("更改界面语言", "Change interface language")
            minWidth = 0
            minimumHeight = dp(40)
            setPadding(dp(14), dp(6), dp(14), dp(6))
            setOnClickListener { showLanguageDialog() }
        })
        root.addView(heading)
        root.addView(title(text(
            "让 Elren 在你明确授权后操作这台 Android 手机。",
            "Let your paired Elren operate this Android phone after you grant access.",
        ), 15f, Color.rgb(75, 85, 99)))

        status = title(text("尚未绑定电脑", "No computer is paired"), 16f)
        status.setPadding(dp(16), dp(16), dp(16), dp(16))
        status.setBackgroundColor(Color.WHITE)
        root.addView(status, flexibleMargins(bottom = 18).apply { topMargin = dp(12) })

        root.addView(Button(this).apply {
            text = text("扫描电脑绑定码", "Scan computer QR code")
            minimumHeight = dp(52)
            setPadding(dp(16), dp(12), dp(16), dp(12))
            setOnClickListener {
                scanLauncher.launch(
                    ScanOptions()
                        .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                        .setPrompt(text("扫描 Elren 中的二维码", "Scan the QR code shown in Elren"))
                        .setBeepEnabled(false)
                        .setOrientationLocked(false),
                )
            }
        }, flexibleMargins())

        connectButton = Button(this).apply {
            text = text("立即连接电脑", "Connect to computer now")
            minimumHeight = dp(52)
            setPadding(dp(16), dp(12), dp(16), dp(12))
            setOnClickListener { startConnectionServiceSafely() }
        }
        root.addView(connectButton, flexibleMargins())

        accessibilityButton = Button(this).apply {
            minimumHeight = dp(52)
            setPadding(dp(16), dp(12), dp(16), dp(12))
            setOnClickListener { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
        }
        root.addView(accessibilityButton, flexibleMargins(bottom = 6))
        accessibilityState = title("", 13f).apply { setPadding(dp(12), dp(8), dp(12), dp(14)) }
        root.addView(accessibilityState, flexibleMargins(bottom = 12))
        updateAccessibilityState()

        root.addView(Button(this).apply {
            text = text("允许读取屏幕（截图时需要）", "Allow screen capture (for screenshots)")
            minimumHeight = dp(52)
            setPadding(dp(16), dp(12), dp(16), dp(12))
            setOnClickListener {
                val manager = getSystemService(MediaProjectionManager::class.java)
                projectionLauncher.launch(manager.createScreenCaptureIntent())
            }
        }, flexibleMargins())
        screenCaptureState = title(
            text("● 尚未允许本次屏幕读取。", "● Screen capture is not enabled for this session."),
            13f,
            Color.rgb(75, 85, 99),
        ).apply { setPadding(dp(12), dp(4), dp(12), dp(12)) }
        root.addView(screenCaptureState, flexibleMargins(bottom = 12))

        root.addView(Switch(this).apply {
            text = text("允许自主模式直接操作本手机", "Allow Autonomous mode to control this phone")
            textSize = 16f
            isChecked = store.autonomousEnabled()
            minimumHeight = dp(56)
            setPadding(dp(8), dp(14), dp(8), dp(14))
            setOnCheckedChangeListener { _: CompoundButton, checked: Boolean ->
                store.setAutonomousEnabled(checked)
            }
        }, flexibleMargins())

        root.addView(title(text(
            "启用后，电脑端任务选择“自主”时，普通点击、滑动、输入、打开应用等操作不再弹出 Elren 审批。Android 系统权限、生物识别、验证码、支付、安全设置和应用安装仍由 Android 或相关应用要求本人确认。连接期间通知栏会持续显示控制状态。",
            "When enabled, ordinary taps, swipes, typing, and app launches do not show Elren approval prompts for tasks in Autonomous mode. Android still requires you for system permission dialogs, biometrics, CAPTCHA, payments, security settings, and app installation. A persistent notification remains visible while connected.",
        ), 13f, Color.rgb(75, 85, 99)))

        root.addView(Button(this).apply {
            text = text("断开并解除绑定", "Disconnect and unpair")
            setTextColor(Color.rgb(185, 28, 28))
            minimumHeight = dp(52)
            setPadding(dp(16), dp(12), dp(16), dp(12))
            setOnClickListener {
                if (unpairingInProgress) return@setOnClickListener
                unpairingInProgress = true
                status.text = text("正在断开并解除绑定…", "Disconnecting and unpairing…")
                // Stop control immediately; the best-effort desktop revocation
                // may legitimately wait for its bounded network timeout.
                stopService(Intent(this@MainActivity, CompanionForegroundService::class.java))
                PairingClient(this@MainActivity).unpair { result ->
                    runOnUiThread {
                        unpairingInProgress = false
                        status.text = if (result.isSuccess) {
                            text("已解除绑定", "Unpaired")
                        } else {
                            text("已从手机解除绑定；电脑当前不可达。", "Unpaired on this phone; the computer is currently unreachable.")
                        }
                        updatePairingState()
                    }
                }
            }
        }, flexibleMargins())

        scroll.addView(root)
        return scroll
    }

    private fun flexibleMargins(bottom: Int = 12) = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
    ).apply {
        setMargins(0, 0, 0, dp(bottom))
        gravity = Gravity.CENTER_HORIZONTAL
    }

    private fun isAccessibilityServiceEnabled(): Boolean {
        val expected = ComponentName(this, ElrenAccessibilityService::class.java)
        val enabled = Settings.Secure.getString(
            contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ).orEmpty()
        return enabled.split(':')
            .mapNotNull(ComponentName::unflattenFromString)
            .any { it == expected }
    }

    private fun updateAccessibilityState() {
        val enabled = isAccessibilityServiceEnabled()
        accessibilityButton.text = if (enabled) {
            text("手机控制辅助功能（已开启）", "Phone-control accessibility (enabled)")
        } else {
            text("开启手机控制辅助功能", "Enable phone-control accessibility")
        }
        accessibilityState.text = if (enabled) {
            text("● 已开启，Elren 可以执行已授权的手机操作。", "● Enabled. Elren can perform authorized phone actions.")
        } else {
            text("● 未开启。点击上方按钮，在系统设置中启用 Elren。", "● Not enabled. Tap the button above and enable Elren in system settings.")
        }
        accessibilityState.setTextColor(
            if (enabled) Color.rgb(22, 135, 82) else Color.rgb(180, 83, 9),
        )
    }

    private fun updateScreenCaptureState() {
        if (!::screenCaptureState.isInitialized || !::store.isInitialized) return
        val enabled = store.screenCaptureActive()
        var phase = store.screenCapturePhase()
        if (enabled) projectionRequestPending = false
        val stateUpdatedAt = store.screenCaptureUpdatedAt()
        val stateAge = if (stateUpdatedAt > 0L) {
            (System.currentTimeMillis() - stateUpdatedAt).coerceAtLeast(0L)
        } else {
            0L
        }
        if (
            !enabled &&
            phase in setOf("grant_received", "starting") &&
            stateUpdatedAt > 0L &&
            stateAge >= 30_000L
        ) {
            projectionRequestPending = false
            ProjectionGrantBroker.discard(store.screenCaptureOwner())
            store.setScreenCaptureState(
                false,
                "SCREEN_CAPTURE_START_TIMEOUT",
                "error",
                ownerId = "",
            )
            phase = "error"
        }
        val waiting = projectionRequestPending &&
            SystemClock.elapsedRealtime() - projectionRequestStartedAt < 30_000L
        val error = store.screenCaptureError()
        val visibleError = if (error == "SCREEN_CAPTURE_START_TIMEOUT") {
            text(
                "系统未及时启动屏幕读取，请重新允许。",
                "Android did not start screen capture in time; allow it again.",
            )
        } else {
            error
        }
        screenCaptureState.text = when {
            enabled -> text("● 已允许本次屏幕读取。", "● Screen capture is enabled for this session.")
            waiting || phase == "grant_received" || phase == "starting" -> text(
                "● 已收到授权，正在建立屏幕读取…",
                "● Permission received; starting screen capture…",
            )
            error.isNotBlank() -> text(
                "● 屏幕读取未能启动：$visibleError",
                "● Screen capture could not start: $visibleError",
            )
            phase == "expired" -> text(
                "● 本次屏幕读取已结束，请重新允许。",
                "● This screen-capture session ended; allow it again.",
            )
            else -> text("● 尚未允许本次屏幕读取。", "● Screen capture is not enabled for this session.")
        }
        screenCaptureState.setTextColor(
            when {
                enabled -> Color.rgb(22, 135, 82)
                waiting || phase == "grant_received" || phase == "starting" -> Color.rgb(75, 85, 99)
                error.isNotBlank() -> Color.rgb(185, 28, 28)
                phase == "expired" -> Color.rgb(180, 83, 9)
                else -> Color.rgb(75, 85, 99)
            },
        )
    }

    private fun pairFromCode(value: String) {
        if (pairingInProgress) return
        pairingInProgress = true
        status.text = text("正在安全绑定…", "Pairing securely…")
        PairingClient(this).pair(value) { result ->
            runOnUiThread {
                pairingInProgress = false
                result.onSuccess {
                    status.text = text("绑定成功，正在建立加密连接…", "Paired; establishing the encrypted connection…")
                    // Some Android/OEM builds do not regard the scanner return
                    // as foreground immediately. Wait for the Activity to be
                    // fully resumed before starting the foreground service.
                    statusHandler.postDelayed({ startConnectionServiceSafely() }, 900)
                }.onFailure {
                    status.text = text("绑定失败：${it.message}", "Pairing failed: ${it.message}")
                }
            }
        }
    }

    private fun connectIfPaired() {
        if (!::store.isInitialized || !store.isPaired()) {
            updatePairingState()
            return
        }
        // The stored state is only a UI snapshot. Android may kill the service
        // process without giving it a chance to persist "offline", so always
        // ask the idempotent service to validate/recreate the live connection
        // when the Activity becomes visible again.
        startConnectionServiceSafely()
    }

    private fun startConnectionServiceSafely() {
        if (!store.isPaired()) {
            updatePairingState()
            return
        }
        // The foreground service is the single owner of connection state. A
        // repeated ACTION_CONNECT is cheap and lets a newly recreated service
        // recover even when the last persisted state still says "online".
        updatePairingState()
        try {
            val intent = Intent(this, CompanionForegroundService::class.java)
                .setAction(CompanionForegroundService.ACTION_CONNECT)
            startForegroundService(intent)
        } catch (error: Throwable) {
            // A foreground-service restriction must never crash the Activity
            // or make the launcher icon appear unusable.
            store.setConnectionState("offline")
            store.setConnectionError("${error.javaClass.simpleName}: ${error.message}")
            updatePairingState()
        }
    }

    private fun updatePairingState() {
        if (!::status.isInitialized || pairingInProgress || unpairingInProgress) return
        if (!store.isPaired()) {
            status.text = text("尚未绑定电脑", "No computer is paired")
            if (::connectButton.isInitialized) connectButton.isEnabled = false
            return
        }
        if (::connectButton.isInitialized) {
            connectButton.isEnabled = store.connectionState() != "online"
            connectButton.text = if (store.connectionState() == "online") {
                text("电脑已连接", "Computer connected")
            } else {
                text("立即连接电脑", "Connect to computer now")
            }
        }
        val lastError = store.connectionError()
        status.text = when (store.connectionState()) {
            "online" -> text("已绑定 · 已连接电脑", "Paired · Connected to computer")
            "connecting" -> text("已绑定 · 正在连接电脑…", "Paired · Connecting to computer…")
            "reconnecting" -> text("已绑定 · 正在重新连接…", "Paired · Reconnecting…")
            else -> if (lastError.isBlank()) {
                text("已绑定 · 当前离线", "Paired · Currently offline")
            } else {
                text("已绑定 · 连接失败：$lastError", "Paired · Connection failed: $lastError")
            }
        }
    }

    private fun showLanguageDialog() {
        val modes = arrayOf("system", "zh", "en")
        val labels = arrayOf(
            text("跟随手机系统", "Follow phone system"),
            "中文",
            "English",
        )
        val selected = modes.indexOf(store.interfaceLanguage()).coerceAtLeast(0)
        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle(text("界面语言", "Interface language"))
            .setSingleChoiceItems(labels, selected) { activeDialog, index ->
                val next = modes[index]
                activeDialog.dismiss()
                if (next != store.interfaceLanguage()) {
                    store.setInterfaceLanguage(next)
                    applyStoredLanguage()
                }
            }
            .setNegativeButton(text("取消", "Cancel"), null)
            .create()
        dialog.show()
    }

    private fun applyStoredLanguage() {
        val languageTags = when (store.interfaceLanguage()) {
            "zh" -> "zh-CN"
            "en" -> "en"
            else -> ""
        }
        val requested = LocaleListCompat.forLanguageTags(languageTags)
        if (AppCompatDelegate.getApplicationLocales() != requested) {
            AppCompatDelegate.setApplicationLocales(requested)
        }
    }
}
