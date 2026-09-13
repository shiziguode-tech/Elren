package ai.elren.mobile

import android.app.NotificationChannel
import android.app.KeyguardManager
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.security.SecureRandom
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.Locale
import java.util.UUID

class CompanionForegroundService : Service() {
    companion object {
        const val ACTION_CONNECT = "ai.elren.mobile.CONNECT"
        const val ACTION_SET_PROJECTION = "ai.elren.mobile.SET_PROJECTION"
        const val ACTION_PROJECTION_STATE_CHANGED = "ai.elren.mobile.PROJECTION_STATE_CHANGED"
        const val ACTION_DISCONNECT = "ai.elren.mobile.DISCONNECT"
        const val ACTION_UNPAIR = "ai.elren.mobile.UNPAIR"
        const val EXTRA_PROJECTION_REQUEST_ID = "projection_request_id"
        const val EXTRA_PROJECTION_ACTIVE = "projection_active"
        const val EXTRA_PROJECTION_ERROR = "projection_error"
        private const val CHANNEL = "elren_phone_control"
        private const val NOTIFICATION_ID = 8101
    }

    private val http = OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS).build()
    private val worker = Executors.newSingleThreadExecutor()
    private lateinit var store: SecureStore
    private lateinit var capture: ScreenCaptureController
    private val reconnectHandler by lazy { Handler(mainLooper) }
    @Volatile private var socket: WebSocket? = null
    @Volatile private var pairedComputer: PairedComputer? = null
    @Volatile private var activePairing: PairedComputer? = null
    @Volatile private var stopping = false
    private var reconnectAttempts = 0
    private var foregroundReady = false
    private lateinit var connectivityManager: ConnectivityManager
    private var networkCallbackRegistered = false
    @Volatile private var projectionOwnerId = ""
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            reconnectHandler.post { reconnectImmediately() }
        }

        override fun onLost(network: Network) {
            reconnectHandler.post {
                if (stopping || pairedComputer == null) return@post
                store.setConnectionState("reconnecting")
                val stale = socket
                socket = null
                stale?.cancel()
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        store = SecureStore(this)
        pairedComputer = store.load()
        // A MediaProjection result is a one-shot, process-local grant. Preserve
        // it only while the matching broker entry still exists. Android may
        // recreate this service after killing the app process; persisted
        // "starting"/"active" state cannot revive that permission and must not
        // leave the UI claiming that capture is still being established.
        val savedCapturePhase = store.screenCapturePhase()
        val savedCaptureOwner = store.screenCaptureOwner()
        val stagedGrantIsAvailable = savedCapturePhase == "grant_received" &&
            ProjectionGrantBroker.has(savedCaptureOwner)
        if (!stagedGrantIsAvailable) {
            val resetPhase = if (
                savedCapturePhase in setOf("grant_received", "starting", "active")
            ) "expired" else "idle"
            store.setScreenCaptureState(false, phase = resetPhase, ownerId = "")
        }
        capture = ScreenCaptureController(this) { active ->
            val owner = projectionOwnerId
            if (active) {
                publishProjectionState(true, phase = "active", ownerId = owner)
            } else {
                publishProjectionState(
                    false,
                    phase = "expired",
                    ownerId = owner,
                    requireOwnerMatch = true,
                )
            }
        }
        createNotificationChannel()
        try {
            startControlForeground(false)
            foregroundReady = true
            connectivityManager = getSystemService(ConnectivityManager::class.java)
            connectivityManager.registerDefaultNetworkCallback(networkCallback)
            networkCallbackRegistered = true
        } catch (error: Throwable) {
            store.setConnectionState("offline")
            store.setConnectionError("Foreground service: ${error.javaClass.simpleName}: ${error.message}")
            store.setScreenCaptureState(
                false,
                "Foreground service: ${error.javaClass.simpleName}: ${error.message}",
                "error",
            )
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!foregroundReady) return START_NOT_STICKY
        try {
            when (intent?.action) {
            ACTION_DISCONNECT -> {
                stopping = true
                reconnectHandler.removeCallbacksAndMessages(null)
                socket?.close(1000, "Disconnected by user")
                stopSelf()
            }
            ACTION_UNPAIR -> {
                stopping = true
                reconnectHandler.removeCallbacksAndMessages(null)
                val paired = pairedComputer ?: store.load()
                val active = socket
                if (paired != null && active != null) {
                    send(active, paired.key, JSONObject().put("type", "unpair"))
                    active.close(1000, "Unpaired by phone")
                }
                store.clear()
                pairedComputer = null
                activePairing = null
                stopSelf()
            }
            ACTION_SET_PROJECTION -> {
                // Android screen-capture consent belongs to this phone and is
                // independent of whether a desktop pairing currently exists.
                // Keeping these concerns separate also lets users re-pair a
                // computer without having to grant MediaProjection again.
                stopping = false
                val requestId = intent.getStringExtra(EXTRA_PROJECTION_REQUEST_ID)
                    .orEmpty()
                    .ifBlank { UUID.randomUUID().toString() }
                projectionOwnerId = requestId
                publishProjectionState(false, phase = "starting", ownerId = requestId)
                val staged = ProjectionGrantBroker.consume(requestId)
                if (staged == null) {
                    publishProjectionState(
                        false,
                        "The one-time Android screen-capture grant was unavailable",
                        "error",
                    )
                } else {
                    try {
                        startControlForeground(true)
                        capture.setProjection(staged.resultCode, staged.data)
                    } catch (error: Throwable) {
                        // A projection failure must not tear down the phone
                        // connection. Report the real error and keep the
                        // ordinary control service alive for a retry.
                        publishProjectionState(
                            false,
                            "${error.javaClass.simpleName}: ${error.message}",
                            "error",
                        )
                        startControlForeground(false)
                    }
                }
                if (pairedComputer == null) pairedComputer = store.load()
                if (pairedComputer != null) connect()
            }
                else -> {
                    pairedComputer = store.load()
                    if (pairedComputer == null) {
                        // START_STICKY may recreate the service after a phone
                        // was unpaired. Do not leave behind a misleading
                        // "connected" foreground notification in that case.
                        store.setConnectionState("offline")
                        stopSelf()
                        return START_NOT_STICKY
                    }
                    stopping = false
                    connect()
                }
            }
        } catch (error: Throwable) {
            store.setConnectionState("offline")
            store.setConnectionError("Connection service: ${error.javaClass.simpleName}: ${error.message}")
            socket?.cancel()
            socket = null
            activePairing = null
            stopSelf()
            return START_NOT_STICKY
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startControlForeground(mediaProjection: Boolean) {
        val openIntent = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
        val open = PendingIntent.getActivity(
            this,
            0,
            openIntent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val disconnect = PendingIntent.getService(
            this,
            1,
            Intent(this, CompanionForegroundService::class.java).setAction(ACTION_DISCONNECT),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification = NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_elren_notification)
            .setContentTitle(localized("Elren 已连接", "Elren is connected"))
            .setContentText(localized("点击查看或断开手机控制", "Tap to view or disconnect phone control"))
            .setContentIntent(open)
            .setOngoing(true)
            .setSilent(true)
            .addAction(0, localized("断开", "Disconnect"), disconnect)
            .build()
        val type = if (mediaProjection && Build.VERSION.SDK_INT >= 29) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
        } else if (Build.VERSION.SDK_INT >= 29) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
        } else {
            0
        }
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, type)
    }

    private fun publishProjectionState(
        active: Boolean,
        error: String = "",
        phase: String = if (active) "active" else if (error.isNotBlank()) "error" else "idle",
        ownerId: String? = null,
        requireOwnerMatch: Boolean = false,
    ) {
        if (requireOwnerMatch && (ownerId.isNullOrBlank() || store.screenCaptureOwner() != ownerId)) {
            return
        }
        store.setScreenCaptureState(active, error, phase, ownerId)
        sendBroadcast(
            Intent(ACTION_PROJECTION_STATE_CHANGED)
                .setPackage(packageName)
                .putExtra(EXTRA_PROJECTION_ACTIVE, active)
                .putExtra(EXTRA_PROJECTION_ERROR, error.take(240)),
        )
    }

    private fun createNotificationChannel() {
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(
                CHANNEL,
                localized("手机控制连接", "Phone control connection"),
                NotificationManager.IMPORTANCE_LOW,
            ),
        )
    }

    private fun localized(zh: String, en: String): String =
        if (store.usesChinese(resources.configuration.locales[0].language)) zh else en

    @Synchronized private fun connect() {
        val paired = pairedComputer ?: store.load()?.also { pairedComputer = it } ?: return
        if (stopping) return
        if (socket != null && samePairing(activePairing, paired)) {
            // ACTION_CONNECT is intentionally idempotent. newWebSocket()
            // returns before the HTTP upgrade finishes, so a non-null socket
            // means either "connecting" or "online". Only onOpen is allowed
            // to claim that the phone is online.
            return
        }
        val superseded = socket
        socket = null
        activePairing = null
        superseded?.cancel()
        store.setConnectionState(if (reconnectAttempts == 0) "connecting" else "reconnecting")
        val timestamp = System.currentTimeMillis() / 1000
        val nonce = ByteArray(24).also(SecureRandom()::nextBytes).let(CryptoProtocol::b64)
        val signature = CryptoProtocol.connectionSignature(paired.key, paired.deviceId, timestamp, nonce)
        val httpUrl = paired.endpoint.toHttpUrl().newBuilder()
            .addPathSegment("ws")
            .addPathSegment(paired.deviceId)
            .addQueryParameter("ts", timestamp.toString())
            .addQueryParameter("nonce", nonce)
            .addQueryParameter("sig", signature)
            .build()
        // OkHttp performs the WebSocket upgrade from an HTTP(S) request. Keep
        // the parsed HttpUrl instead of manufacturing a ws:// string, which
        // older/newer HttpUrl parsers may reject as an unexpected scheme.
        val nextSocket = http.newWebSocket(Request.Builder().url(httpUrl).build(), object : WebSocketListener() {
            private var opened = false

            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (socket !== webSocket || !samePairing(activePairing, paired)) {
                    webSocket.close(1000, "Superseded pairing")
                    return
                }
                opened = true
                reconnectAttempts = 0
                store.clearConnectionError()
                store.setConnectionState("online")
                send(
                    webSocket,
                    paired.key,
                    JSONObject()
                        .put("type", "hello")
                        .put("capabilities", JSONArray(listOf(
                            "list_apps", "inspect", "screenshot", "tap", "scroll", "swipe",
                            "type", "back", "home", "recents", "launch", "open_url",
                        ))),
                )
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (socket !== webSocket || !samePairing(activePairing, paired)) return
                try {
                    val payload = CryptoProtocol.decrypt(paired.key, JSONObject(text))
                    when (payload.optString("type")) {
                        "command" -> worker.execute { executeCommand(webSocket, paired.key, payload) }
                        "pong", "hello" -> Unit
                    }
                } catch (_: Exception) {
                    webSocket.close(4002, "Invalid encrypted packet")
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (socket !== webSocket || !samePairing(activePairing, paired)) return
                // Only an explicit revocation received after a completed
                // WebSocket handshake removes the phone-side pairing.
                if (opened && code == 4003) {
                    stopping = true
                    store.clear()
                    pairedComputer = null
                    activePairing = null
                    socket = null
                    stopSelf()
                } else {
                    disconnected(webSocket)
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                // A PC restart, Wi-Fi handover, or failed upgrade is temporary.
                // Never delete an encrypted pairing because of a transport error.
                if (!stopping && socket === webSocket && samePairing(activePairing, paired)) {
                    val http = response?.code?.let { "HTTP $it - " }.orEmpty()
                    store.setConnectionError("$http${t.javaClass.simpleName}: ${t.message}")
                }
                disconnected(webSocket)
            }
        })
        activePairing = paired
        socket = nextSocket
    }

    private fun samePairing(left: PairedComputer?, right: PairedComputer?): Boolean =
        left != null && right != null &&
            left.endpoint == right.endpoint &&
            left.deviceId == right.deviceId &&
            left.key.contentEquals(right.key)

    @Synchronized private fun reconnectImmediately() {
        if (stopping || pairedComputer == null) return
        reconnectHandler.removeCallbacksAndMessages(null)
        val stale = socket
        socket = null
        activePairing = null
        stale?.cancel()
        reconnectAttempts = maxOf(reconnectAttempts, 1)
        connect()
    }

    @Synchronized private fun disconnected(closed: WebSocket) {
        if (socket !== closed) return
        socket = null
        activePairing = null
        if (!stopping && pairedComputer != null) {
            store.setConnectionState("reconnecting")
            val delay = minOf(30_000L, 1_500L shl minOf(reconnectAttempts, 4))
            reconnectAttempts += 1
            reconnectHandler.removeCallbacksAndMessages(null)
            reconnectHandler.postDelayed({ connect() }, delay)
        }
    }

    private fun executeCommand(webSocket: WebSocket, key: ByteArray, command: JSONObject) {
        val id = command.optString("id")
        val reply = JSONObject().put("type", "result").put("id", id)
        try {
            if (command.optBoolean("autonomous") && !store.autonomousEnabled()) {
                error("Autonomous phone control is disabled on the phone")
            }
            val action = command.getString("action")
            val args = command.optJSONObject("arguments") ?: JSONObject()
            val result = when (action) {
                "status" -> JSONObject()
                    .put("connected", true)
                    .put("autonomous_enabled", store.autonomousEnabled())
                    .put("accessibility_enabled", ElrenAccessibilityService.instance != null)
                "list_apps" -> listInstalledApps(args.optBoolean("include_system", false))
                "screenshot" -> {
                    val keyguard = getSystemService(KeyguardManager::class.java)
                    if (keyguard.isDeviceLocked) {
                        error("The phone is locked; screenshot is blocked and user takeover is required")
                    }
                    val accessibility = ElrenAccessibilityService.instance
                        ?: error("Phone-control accessibility is required before screenshots can be captured safely")
                    if (accessibility.requiresUserAuthentication()) {
                        error("Authentication or verification-code screen detected; screenshot is blocked and user takeover is required")
                    }
                    JSONObject().put("image_base64", capture.captureBase64()).put("format", "png")
                }
                else -> {
                    val accessibility = ElrenAccessibilityService.instance
                        ?: error("Elren phone-control accessibility is not enabled")
                    val actionResult = accessibility.execute(action, args)
                    if (action in setOf("scroll", "swipe") && actionResult.optBoolean("accepted")) {
                        val duration = if (action == "scroll") {
                            actionResult.optLong("duration_ms", 900)
                        } else {
                            args.optLong("duration_ms", 450)
                        }
                        val stability = accessibility.waitForUiStability(duration + 180)
                        actionResult.put("ui_stability", stability)
                        if (action == "scroll") {
                            val after = accessibility.execute("visible_content", JSONObject())
                                .optJSONArray("items") ?: JSONArray()
                            val before = actionResult.optJSONArray("before_visible_items") ?: JSONArray()
                            val beforeValues = (0 until before.length())
                                .map { before.optString(it) }.filter(String::isNotBlank).toSet()
                            val afterValues = (0 until after.length())
                                .map { after.optString(it) }.filter(String::isNotBlank)
                            val overlap = afterValues.filter(beforeValues::contains)
                            val fresh = afterValues.filterNot(beforeValues::contains)
                            val denominator = minOf(beforeValues.size, afterValues.size)
                                .coerceAtLeast(1)
                            val observedOverlap = overlap.size.toDouble() / denominator
                            val direction = actionResult.optString("direction", "down")
                            val nextOverlap = accessibility.updateAdaptiveOverlap(
                                direction,
                                actionResult.optDouble("overlap_target", 0.45),
                                observedOverlap,
                                fresh.size,
                            )
                            actionResult
                                .put("after_visible_items", after)
                                .put("overlap_items", JSONArray(overlap))
                                .put("new_visible_items", JSONArray(fresh))
                                .put("at_boundary", afterValues.toSet() == beforeValues)
                                .put("observed_overlap_ratio", observedOverlap)
                                .put("next_overlap_ratio", nextOverlap)
                                .put(
                                    "reading_hint",
                                    "Use new_visible_items for the next reading segment and overlap_items only as continuity anchors.",
                                )
                        }
                    }
                    actionResult
                }
            }
            reply.put("ok", true).put("result", result)
        } catch (error: Throwable) {
            reply.put("ok", false).put("error", "${error.javaClass.simpleName}: ${error.message}")
        }
        send(webSocket, key, reply)
    }

    @Suppress("DEPRECATION")
    private fun listInstalledApps(includeSystem: Boolean): JSONObject {
        val launcherIntent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val resolved = if (Build.VERSION.SDK_INT >= 33) {
            packageManager.queryIntentActivities(
                launcherIntent,
                PackageManager.ResolveInfoFlags.of(0L),
            )
        } else {
            packageManager.queryIntentActivities(launcherIntent, 0)
        }
        val applications = resolved.map { it.activityInfo.applicationInfo }
            .distinctBy { it.packageName }
            .mapNotNull { info ->
            val system = info.flags and ApplicationInfo.FLAG_SYSTEM != 0 ||
                info.flags and ApplicationInfo.FLAG_UPDATED_SYSTEM_APP != 0
            if (!includeSystem && system) return@mapNotNull null
            val label = runCatching {
                packageManager.getApplicationLabel(info).toString().trim()
            }.getOrDefault("")
            JSONObject()
                .put("name", label.ifEmpty { info.packageName })
                .put("package", info.packageName)
                .put("system", system)
                .put("enabled", info.enabled)
        }.sortedWith(
            compareBy<JSONObject>(
                { it.optString("name").lowercase(Locale.ROOT) },
                { it.optString("package") },
            ),
        )
        return JSONObject()
            .put("source", "android_package_manager")
            .put("scope", "user_visible_launcher_apps")
            .put("complete_for_scope", true)
            .put("include_system", includeSystem)
            .put("count", applications.size)
            .put("apps", JSONArray(applications))
            .put(
                "reading_hint",
                if (includeSystem) {
                    "Complete user-visible launcher app inventory, including system launcher apps."
                } else {
                    "Complete non-system user-visible app inventory; no Settings navigation or scrolling is needed."
                },
            )
    }

    private fun send(webSocket: WebSocket, key: ByteArray, payload: JSONObject) {
        webSocket.send(CryptoProtocol.encrypt(key, payload).toString())
    }

    override fun onDestroy() {
        stopping = true
        reconnectHandler.removeCallbacksAndMessages(null)
        if (networkCallbackRegistered) {
            runCatching { connectivityManager.unregisterNetworkCallback(networkCallback) }
            networkCallbackRegistered = false
        }
        socket?.close(1000, "Service stopped")
        socket = null
        activePairing = null
        capture.close()
        worker.shutdownNow()
        if (pairedComputer != null) {
            store.setConnectionState("offline")
        }
        super.onDestroy()
    }
}
