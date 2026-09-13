from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepdesk.mobile_bridge import (
    MobileBridge,
    PendingPairing,
    connection_signature,
    decrypt_packet,
    derive_device_key,
    encrypt_packet,
    pairing_proof,
    preferred_lan_address,
    revocation_signature,
)
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.mobile_device import MobileDeviceTool
from deepdesk.secret_storage import (
    AesGcmProtector,
    LocalSecretVault,
    SecretStorageUnavailable,
)


def test_mobile_crypto_round_trip_and_tamper_rejection() -> None:
    secret = bytes(range(32))
    key = derive_device_key(secret, "pairing", "android-device")
    assert len(key) == 32
    assert key == derive_device_key(secret, "pairing", "android-device")
    assert pairing_proof(secret, "pairing", "android-device", "nonce-value-12345") != pairing_proof(
        secret, "pairing", "another-device", "nonce-value-12345"
    )
    assert connection_signature(key, "android-device", 123, "nonce-value-12345")
    assert revocation_signature(key, "android-device", 123, "nonce-value-12345")
    assert revocation_signature(key, "android-device", 123, "nonce-value-12345") != connection_signature(
        key, "android-device", 123, "nonce-value-12345"
    )
    envelope = encrypt_packet(key, {"type": "command", "text": "中文 ✓"})
    assert decrypt_packet(key, envelope)["text"] == "中文 ✓"
    tampered = dict(envelope)
    raw = bytearray(base64.urlsafe_b64decode(tampered["ciphertext"] + "=" * (-len(tampered["ciphertext"]) % 4)))
    raw[0] ^= 1
    tampered["ciphertext"] = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(ValueError):
        decrypt_packet(key, tampered)


def test_pairing_payload_is_expiring_and_secret_is_not_in_status(tmp_path: Path) -> None:
    bridge = MobileBridge(tmp_path / "data", tmp_path / "screenshots", port=18768)
    pending = PendingPairing("pairing-id", b"x" * 32, "http://192.168.1.7:18768")
    bridge.pairings[pending.pairing_id] = pending
    payload = bridge.pairing_payload(pending.pairing_id)
    assert payload.startswith("elren://pair?")
    assert "secret=" in payload
    public = bridge.status()
    assert "secret" not in str(public).lower()
    assert "key_local" not in str(public)
    assert "key_dpapi" not in str(public)


def test_plaintext_key_local_is_quarantined_outside_workspace_and_requires_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True)
    quarantine = tmp_path / "secure" / "legacy-mobile.vault"
    monkeypatch.setattr(
        "deepdesk.mobile_bridge._legacy_mobile_quarantine_path",
        lambda _data_dir: quarantine,
    )
    protector = AesGcmProtector("test-mobile-quarantine", b"q" * 32)
    encoded_key = base64.urlsafe_b64encode(b"x" * 32).decode()
    (data_dir / "mobile-devices.json").write_text(
        '{"schema": 1, "devices": [{"device_id": "android-legacy", '
        '"name": "old", "key_local": "' + encoded_key + '"}]}',
        encoding="utf-8",
    )

    bridge = MobileBridge(
        data_dir,
        workspace / "screenshots",
        secret_protector=protector,
    )

    assert "android-legacy" not in bridge._secret_values
    assert "android-legacy" not in bridge._records
    assert bridge.has_devices is False
    with pytest.raises(KeyError):
        bridge._device_key("android-legacy")
    active = (data_dir / "mobile-devices.json").read_text(encoding="utf-8")
    assert "key_local" not in active
    assert encoded_key not in active
    assert not (data_dir / "mobile-device-secrets.vault").exists()
    assert quarantine.is_file()
    assert workspace not in quarantine.parents
    assert encoded_key.encode() not in quarantine.read_bytes()
    quarantined = LocalSecretVault(quarantine, protector).read().values
    assert quarantined["devices"]["android-legacy"]["key_local"] == encoded_key

    restarted = MobileBridge(
        data_dir,
        workspace / "screenshots",
        secret_protector=protector,
    )
    assert restarted.has_devices is False
    assert restarted.status()["legacy_devices_quarantined"] == 0


def test_plaintext_key_local_is_removed_even_when_secure_quarantine_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class UnavailableProtector:
        algorithm = "unavailable-test-protector"

        def protect(self, _plaintext: bytes) -> bytes:
            raise SecretStorageUnavailable("test protector unavailable")

        def unprotect(self, _protected: bytes) -> bytes:
            raise SecretStorageUnavailable("test protector unavailable")

    workspace = tmp_path / "workspace"
    data_dir = workspace / "data"
    data_dir.mkdir(parents=True)
    quarantine = tmp_path / "secure" / "legacy-mobile.vault"
    monkeypatch.setattr(
        "deepdesk.mobile_bridge._legacy_mobile_quarantine_path",
        lambda _data_dir: quarantine,
    )
    encoded_key = base64.urlsafe_b64encode(b"y" * 32).decode()
    (data_dir / "mobile-devices.json").write_text(
        '{"schema": 1, "devices": [{"device_id": "android-legacy", '
        '"name": "old", "key_local": "' + encoded_key + '"}]}',
        encoding="utf-8",
    )

    bridge = MobileBridge(
        data_dir,
        workspace / "screenshots",
        secret_protector=UnavailableProtector(),
    )

    active = (data_dir / "mobile-devices.json").read_text(encoding="utf-8")
    assert bridge.has_devices is False
    assert "key_local" not in active
    assert encoded_key not in active
    assert not quarantine.exists()
    assert bridge.status()["credential_store_ready"] is False


def test_mobile_connection_nonce_is_single_use(tmp_path: Path) -> None:
    bridge = MobileBridge(tmp_path / "data", tmp_path / "screenshots")

    assert bridge._consume_connection_nonce("android-device", "fresh-nonce-value") is True
    assert bridge._consume_connection_nonce("android-device", "fresh-nonce-value") is False
    assert bridge._consume_connection_nonce("another-device", "fresh-nonce-value") is True


@pytest.mark.asyncio
async def test_mobile_disconnect_fails_pending_command_without_waiting_for_timeout(
    tmp_path: Path,
) -> None:
    bridge = MobileBridge(tmp_path / "data", tmp_path / "screenshots")
    future = asyncio.get_running_loop().create_future()
    bridge.pending_commands["request"] = ("android-device", future)

    bridge._fail_pending_for_device("android-device", "phone disconnected")

    with pytest.raises(ConnectionError, match="phone disconnected"):
        await future


def test_preferred_lan_address_prefers_physical_lan_over_virtual_default_route(monkeypatch) -> None:
    monkeypatch.setattr(
        "deepdesk.mobile_bridge.psutil.net_if_addrs",
        lambda: {
            "vEthernet (WSL)": [SimpleNamespace(family=socket.AF_INET, address="172.20.0.1")],
            "Wi-Fi": [SimpleNamespace(family=socket.AF_INET, address="192.168.50.7")],
        },
    )
    monkeypatch.setattr(
        "deepdesk.mobile_bridge.psutil.net_if_stats",
        lambda: {
            "vEthernet (WSL)": SimpleNamespace(isup=True),
            "Wi-Fi": SimpleNamespace(isup=True),
        },
    )
    monkeypatch.setattr("deepdesk.mobile_bridge._default_route_address", lambda: "172.20.0.1")

    assert preferred_lan_address() == "192.168.50.7"


def test_preferred_lan_address_rejects_public_and_link_local_addresses(monkeypatch) -> None:
    monkeypatch.setattr(
        "deepdesk.mobile_bridge.psutil.net_if_addrs",
        lambda: {
            "Ethernet": [
                SimpleNamespace(family=socket.AF_INET, address="169.254.4.2"),
                SimpleNamespace(family=socket.AF_INET, address="203.0.113.8"),
            ]
        },
    )
    monkeypatch.setattr(
        "deepdesk.mobile_bridge.psutil.net_if_stats",
        lambda: {"Ethernet": SimpleNamespace(isup=True)},
    )
    monkeypatch.setattr("deepdesk.mobile_bridge._default_route_address", lambda: "203.0.113.8")

    assert preferred_lan_address() == ""


@pytest.mark.asyncio
async def test_pairing_endpoint_starts_and_generates_a_real_qr_png(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    bridge = MobileBridge(tmp_path / "data", tmp_path / "screenshots", port=port)
    try:
        pairing = await bridge.create_pairing()
        assert pairing["endpoint"].endswith(f":{port}")
        assert pairing["qr_url"].endswith(f"/{pairing['pairing_id']}/qr.png")
        png = bridge.pairing_qr_png(pairing["pairing_id"])
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(png) > 500
        assert bridge.status()["ready"] is True
    finally:
        await bridge.stop()


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def status(self) -> dict:
        return {"paired": 1, "connected": 1, "devices": []}

    async def command(self, action, arguments, *, device_id, autonomous):
        self.calls.append(
            {
                "action": action,
                "arguments": arguments,
                "device_id": device_id,
                "autonomous": autonomous,
            }
        )
        return {"accepted": True}


@pytest.mark.asyncio
async def test_mobile_tool_only_marks_commands_autonomous_for_autonomous_tasks(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    tool = MobileDeviceTool(bridge, tmp_path / "screenshots")
    assert tool.risk({"action": "status"}) is Risk.SAFE
    assert tool.risk({"action": "screenshot"}) is Risk.MEDIUM
    assert tool.risk({"action": "tap"}) is Risk.HIGH

    await tool.execute(
        {"action": "tap", "x": 10, "y": 20},
        ToolContext(task_id="a", workspace=str(tmp_path), approval_policy="autonomous"),
    )
    await tool.execute(
        {"action": "tap", "x": 30, "y": 40},
        ToolContext(task_id="b", workspace=str(tmp_path), approval_policy="balanced"),
    )
    assert bridge.calls[0]["autonomous"] is True
    assert bridge.calls[1]["autonomous"] is False


@pytest.mark.asyncio
async def test_mobile_tool_routes_installed_app_inventory_directly(tmp_path: Path) -> None:
    bridge = _FakeBridge()
    tool = MobileDeviceTool(bridge, tmp_path / "screenshots")

    assert "list_apps" in tool.parameters["properties"]["action"]["enum"]
    assert tool.risk({"action": "list_apps"}) is Risk.MEDIUM
    await tool.execute(
        {"action": "list_apps", "device_id": "phone", "include_system": False},
        ToolContext(task_id="apps", workspace=str(tmp_path), approval_policy="autonomous"),
    )

    assert bridge.calls == [
        {
            "action": "list_apps",
            "arguments": {"include_system": False},
            "device_id": "phone",
            "autonomous": True,
        }
    ]


def test_mobile_plugin_and_routes_are_wired_into_the_host() -> None:
    source = Path("deepdesk/main.py").read_text(encoding="utf-8")
    assert "MobileDeviceTool(mobile_bridge" in source
    assert '@app.post("/api/mobile/pairing")' in source
    assert '@app.delete("/api/mobile/devices/{device_id}")' in source
    assert '"mobile": mobile_bridge.status()' in source
    assert '"already_unpaired": not revoked' in source


def test_mobile_pairing_ui_and_bilingual_labels_are_present() -> None:
    index = Path("deepdesk/static/index.html").read_text(encoding="utf-8")
    app = Path("deepdesk/static/app.js").read_text(encoding="utf-8")
    css = Path("deepdesk/static/professional-ui.css").read_text(encoding="utf-8")

    for identifier in (
        'id="pairMobileDevice"',
        'id="mobileDeviceList"',
        'id="mobilePairingDialog"',
        'id="mobilePairingContent"',
    ):
        assert identifier in index
    assert 'setText(".mobile-setting > span", "Android phone control")' in app
    assert 'setText("#pairMobileDevice", "Pair Android phone")' in app
    assert "let mobilePairingPollTimer = null;" in app
    assert "await askForConfirmation(" in app
    assert ".mobile-pair-button" in css
    assert ".mobile-pairing-shell" in css
    assert ".mobile-device-card" in css


def test_android_companion_reports_accessibility_state_and_avoids_fixed_height_clipping() -> None:
    source = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/MainActivity.kt"
    ).read_text(encoding="utf-8")
    manifest = Path("mobile/ElrenMobile/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")

    assert "override fun onResume()" in source
    assert "override fun onResume()" in source
    assert "Waiting until after onResume" in source
    assert "isAccessibilityServiceEnabled()" in source
    assert "Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES" in source
    assert "手机控制辅助功能（已开启）" in source
    assert "ViewGroup.LayoutParams.WRAP_CONTENT" in source
    assert 'store.setConnectionState("online")' in Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/CompanionForegroundService.kt"
    ).read_text(encoding="utf-8")
    assert "fun unpair(" in Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/PairingClient.kt"
    ).read_text(encoding="utf-8")
    assert "正在重新连接" in source
    assert "screenCaptureState" in source
    capture = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/ScreenCaptureController.kt"
    ).read_text(encoding="utf-8")
    service = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/CompanionForegroundService.kt"
    ).read_text(encoding="utf-8")
    store = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/SecureStore.kt"
    ).read_text(encoding="utf-8")
    assert capture.count("createVirtualDisplay(") == 1
    assert "private var display: VirtualDisplay?" in capture
    assert "private var reader: ImageReader?" in capture
    assert "SCREEN_CAPTURE_PERMISSION_REQUIRED" in capture
    assert "private fun publishProjectionState(" in service
    assert "ACTION_PROJECTION_STATE_CHANGED" in service
    assert "projectionRequestPending" in source
    assert "ContextCompat.registerReceiver" in source
    assert "screenCaptureError()" in source
    assert "ProjectionGrantBroker.stage" in source
    assert "screenCapturePhase()" in source
    assert "EXTRA_PROJECTION_REQUEST_ID" in service
    assert "ProjectionGrantBroker.consume" in service
    broker = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/ProjectionGrantBroker.kt"
    ).read_text(encoding="utf-8")
    assert "fun has(requestId: String)" in broker
    assert "stagedGrantIsAvailable" in service
    assert 'savedCapturePhase in setOf("grant_received", "starting", "active")' in service
    assert '"expired" else "idle"' in service
    assert "EXTRA_RESULT_DATA" not in service
    assert "requireOwnerMatch = true" in service
    assert "screenCaptureOwner()" in service
    assert "screenCaptureUpdatedAt()" in store
    assert '"screen_capture_updated_at"' in store
    assert 'stateAge >= 30_000L' in source
    assert '"SCREEN_CAPTURE_START_TIMEOUT"' in source
    assert "Pair this phone before enabling screen capture" not in service
    assert "if (pairedComputer != null) connect()" in service
    assert "Screen capture could not start" in source
    assert "expectedProjection = activeProjection" in capture
    assert "projection !== expectedProjection" in capture
    assert "projection = activeProjection" in capture
    assert "updateScreenCaptureState()" in source
    assert "matchMargins()" not in source
    assert 'android.permission.CAMERA' in manifest
    assert 'android.permission.FOREGROUND_SERVICE_CONNECTED_DEVICE' in manifest
    assert 'android.permission.CHANGE_NETWORK_STATE' in manifest
    assert 'android:foregroundServiceType="connectedDevice|mediaProjection"' in manifest
    assert 'FOREGROUND_SERVICE_DATA_SYNC' not in manifest
    assert 'android:launchMode="singleTask"' in manifest
    assert 'android.intent.category.BROWSABLE' not in manifest
    assert 'android.intent.action.VIEW' not in manifest


def test_android_semantic_scroll_is_viewport_aware_and_waits_for_inertia() -> None:
    accessibility = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/"
        "ElrenAccessibilityService.kt"
    ).read_text(encoding="utf-8")
    service = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/"
        "CompanionForegroundService.kt"
    ).read_text(encoding="utf-8")

    assert "scroll" in MobileDeviceTool.parameters["properties"]["action"]["enum"]
    assert MobileDeviceTool.parameters["properties"]["overlap_ratio"]["minimum"] == 0.25
    assert "always use the semantic scroll action" in MobileDeviceTool.description
    assert '"scroll" -> scrollPage(args)' in accessibility
    assert "largestScrollable(rootInActiveWindow)" in accessibility
    assert 'args.optDouble("overlap_ratio", 0.45)' in accessibility
    assert "waitForUiStability" in accessibility
    assert "TYPE_VIEW_SCROLLED" in accessibility
    assert "updateAdaptiveOverlap" in accessibility
    assert "adaptiveOverlap[direction]" in accessibility
    assert 'action in setOf("scroll", "swipe")' in service
    assert 'put("new_visible_items"' in service
    assert 'put("overlap_items"' in service
    assert 'put("at_boundary"' in service
    assert 'put("next_overlap_ratio"' in service
    assert "depth > 20 || seen++ >= 2_000" in accessibility
    assert "never converts a" in accessibility


def test_android_lists_non_system_apps_via_package_manager_without_ui_navigation() -> None:
    manifest = Path(
        "mobile/ElrenMobile/app/src/main/AndroidManifest.xml"
    ).read_text(encoding="utf-8")
    service = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/"
        "CompanionForegroundService.kt"
    ).read_text(encoding="utf-8")
    engine = Path("deepdesk/engine.py").read_text(encoding="utf-8")

    assert "android.permission.QUERY_ALL_PACKAGES" not in manifest
    assert "android.intent.category.LAUNCHER" in manifest
    assert '"list_apps" -> listInstalledApps' in service
    assert "queryIntentActivities" in service
    assert "ApplicationInfo.FLAG_SYSTEM" in service
    assert "ApplicationInfo.FLAG_UPDATED_SYSTEM_APP" in service
    assert 'put("source", "android_package_manager")' in service
    assert 'put("scope", "user_visible_launcher_apps")' in service
    assert "Complete installed package inventory" not in service
    assert "including system launcher apps" in service
    assert "mobile_device.list_apps" in engine
    assert "do not open Android Settings" in engine


def test_android_companion_preserves_pairing_across_transient_connection_failures() -> None:
    service = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/CompanionForegroundService.kt"
    ).read_text(encoding="utf-8")
    failure_body = service.split("override fun onFailure", 1)[1].split(
        "@Synchronized private fun disconnected", 1
    )[0]

    assert "disconnected(webSocket)" in failure_body
    assert "store.clear()" not in failure_body
    assert "FLAG_ACTIVITY_CLEAR_TOP" in service
    assert "FLAG_ACTIVITY_SINGLE_TOP" in service
    assert "reconnectAttempts" in service
    assert "registerDefaultNetworkCallback(networkCallback)" in service
    assert "reconnectImmediately()" in service
    assert "unregisterNetworkCallback(networkCallback)" in service
    assert "foregroundReady" in service
    assert 'store.setConnectionError("Foreground service:' in service
    assert "Never delete an encrypted pairing" in service
    assert '.scheme(if (paired.endpoint.startsWith("https:"))' not in service
    assert 'Request.Builder().url(httpUrl)' in service
    assert 'val socketUrl = httpUrl.toString().replaceFirst(' not in service
    duplicate_connect = service.split("if (socket != null && samePairing", 1)[1].split(
        'store.setConnectionState(if (reconnectAttempts', 1
    )[0]
    assert 'store.setConnectionState("online")' not in duplicate_connect
    assert "Only onOpen is allowed" in duplicate_connect
    assert 'if (pairedComputer != null) {' in service
    assert "samePairing(activePairing, paired)" in service
    assert "socket !== webSocket" in service
    assert "Superseded pairing" in service
    assert 'store.connectionState() == "online"' not in service
    assert 'return START_NOT_STICKY' in service
    assert 'socket?.cancel()' in service
    assert 'getSystemService(KeyguardManager::class.java)' in service
    assert 'Phone-control accessibility is required before screenshots' in service


def test_phone_and_desktop_unpair_paths_provide_visible_feedback() -> None:
    client = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/PairingClient.kt"
    ).read_text(encoding="utf-8")
    app = Path("deepdesk/static/app.js").read_text(encoding="utf-8")

    assert "store.clear()" in client
    activity = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/MainActivity.kt"
    ).read_text(encoding="utf-8")
    assert "unpairingInProgress" in activity
    assert "if (!::status.isInitialized || pairingInProgress || unpairingInProgress)" in activity
    unpair_handler = activity.split(
        'text = text("断开并解除绑定", "Disconnect and unpair")', 1
    )[1].split("scroll.addView(root)", 1)[0]
    assert unpair_handler.index("stopService(") < unpair_handler.index(".unpair {")
    assert 'title: uiText("解除手机绑定", "Unpair phone")' in app
    assert 'confirmLabel: uiText("解除绑定", "Unpair")' in app
    assert "if (!confirmed) return;" in app
    assert 'button.textContent = uiText("正在解除…", "Unpairing…")' in app
    assert "if (status.mobile) renderMobileDevices(status.mobile);" in app


def test_android_language_defaults_to_system_and_supports_persistent_override() -> None:
    source = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/MainActivity.kt"
    ).read_text(encoding="utf-8")
    store = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/SecureStore.kt"
    ).read_text(encoding="utf-8")
    service = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/"
        "CompanionForegroundService.kt"
    ).read_text(encoding="utf-8")
    default_strings = Path(
        "mobile/ElrenMobile/app/src/main/res/values/strings.xml"
    ).read_text(encoding="utf-8")
    chinese_strings = Path(
        "mobile/ElrenMobile/app/src/main/res/values-zh-rCN/strings.xml"
    ).read_text(encoding="utf-8")
    generic_chinese_strings = Path(
        "mobile/ElrenMobile/app/src/main/res/values-zh/strings.xml"
    ).read_text(encoding="utf-8")
    manifest = Path("mobile/ElrenMobile/app/src/main/AndroidManifest.xml").read_text(
        encoding="utf-8"
    )
    build = Path("mobile/ElrenMobile/app/build.gradle.kts").read_text(encoding="utf-8")

    assert '<string name="app_name">Elren</string>' in default_strings
    assert '<string name="app_name">Elren</string>' in chinese_strings
    assert '<string name="app_name">Elren</string>' in generic_chinese_strings
    assert 'applicationId = "ai.elren.mobile"' in build
    assert 'namespace = "ai.elren.mobile"' in build
    assert "versionCode = 11" in build
    assert 'android:icon="@mipmap/ic_elren_launcher"' in manifest
    assert 'android:roundIcon="@mipmap/ic_elren_launcher"' in manifest
    assert "Milo" not in default_strings + chinese_strings + generic_chinese_strings
    assert "Qelvane" not in default_strings + chinese_strings + generic_chinese_strings
    assert "setScreenCaptureActive" not in Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/SecureStore.kt"
    ).read_text(encoding="utf-8")
    assert "testImplementation" not in Path(
        "mobile/ElrenMobile/app/build.gradle.kts"
    ).read_text(encoding="utf-8")
    assert 'prefs.getString("interface_language", "system")' in store
    assert 'value in setOf("system", "zh", "en")' in store
    assert "fun usesChinese(systemLanguage: String)" in store
    assert 'val modes = arrayOf("system", "zh", "en")' in source
    assert 'setTitle(text("界面语言", "Interface language"))' in source
    assert "store.setInterfaceLanguage(next)" in source
    assert "AppCompatDelegate.setApplicationLocales(requested)" in source
    assert "MaterialAlertDialogBuilder(this)" in source
    assert 'localized("断开", "Disconnect")' in service
    assert 'android:localeConfig="@xml/locales_config"' in manifest
    assert 'xmlns:tools="http://schemas.android.com/tools"' in manifest
    assert 'tools:targetApi="tiramisu"' in manifest


def test_android_preferences_avoid_blocking_writes_except_for_projection_handoff() -> None:
    store = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/SecureStore.kt"
    ).read_text(encoding="utf-8")

    migration = store.split("if (endpoint != savedEndpoint)", 1)[1].split(
        'val deviceId = prefs.getString("paired_device_id"', 1
    )[0]
    clear = store.split("fun clear()", 1)[1].split("fun connectionState()", 1)[0]
    projection = store.split("fun setScreenCaptureState(", 1)[1].split(
        "fun autonomousEnabled()", 1
    )[0]

    assert ".apply()" in migration
    assert ".commit()" not in migration
    assert ".apply()" in clear
    assert ".commit()" not in clear
    assert '@SuppressLint("ApplySharedPref")\n    fun setScreenCaptureState(' in store
    assert "editor.commit()" in projection
    assert "consumed immediately by the foreground Activity" in projection


def test_android_lan_transport_remains_available_with_application_layer_crypto() -> None:
    manifest = Path("mobile/ElrenMobile/app/src/main/AndroidManifest.xml").read_text(
        encoding="utf-8"
    )
    network = Path(
        "mobile/ElrenMobile/app/src/main/res/xml/network_security_config.xml"
    ).read_text(encoding="utf-8")
    protocol = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/CryptoProtocol.kt"
    ).read_text(encoding="utf-8")
    store = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/SecureStore.kt"
    ).read_text(encoding="utf-8")

    # QR pairing targets arbitrary private-LAN IPs, so Android's domain-scoped
    # cleartext allow-list cannot express the product's required endpoint set.
    assert 'android:networkSecurityConfig="@xml/network_security_config"' in manifest
    assert 'cleartextTrafficPermitted="true"' in network
    assert 'Mac.getInstance("HmacSHA256")' in protocol
    assert protocol.count('Cipher.getInstance("AES/GCM/NoPadding")') == 2
    assert "GCMParameterSpec(128, nonce)" in protocol
    assert 'private val aad = "elren-mobile-v1"' in protocol
    assert "require(nonce.size == 12 && tag.size == 16" in protocol
    assert 'KeyStore.getInstance("AndroidKeyStore")' in store
    assert 'Cipher.getInstance("AES/GCM/NoPadding")' in store


def test_android_project_has_a_reproducible_gradle_wrapper() -> None:
    root = Path("mobile/ElrenMobile")
    properties = (root / "gradle/wrapper/gradle-wrapper.properties").read_text("utf-8")

    assert (root / "gradlew").is_file()
    assert (root / "gradlew.bat").is_file()
    assert (root / "gradle/wrapper/gradle-wrapper.jar").is_file()
    assert "org.gradle.wrapper.GradleWrapperMain" in (root / "gradlew").read_text("utf-8")
    assert "-classpath" in (root / "gradlew.bat").read_text("utf-8")
    assert ' -jar "%APP_HOME%\\gradle\\wrapper\\gradle-wrapper.jar"' not in (
        root / "gradlew.bat"
    ).read_text("utf-8")
    assert "gradle-8.9-bin.zip" in properties
    assert (
        "distributionSha256Sum="
        "d725d707bfabd4dfdc958c624003b3c80accc03f7037b5122c4b1d0ef15cecab"
        in properties
    )
    assert "validateDistributionUrl=true" in properties
    assert (
        hashlib.sha256((root / "gradle/wrapper/gradle-wrapper.jar").read_bytes()).hexdigest()
        == "498495120a03b9a6ab5d155f5de3c8f0d986a449153702fb80fc80e134484f17"
    )
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "mobile\\ElrenMobile\\gradlew.bat -p mobile\\ElrenMobile" in readme
    assert "./mobile/ElrenMobile/gradlew -p mobile/ElrenMobile" in readme
    assert "work/openclaw-reference/apps/android/gradlew" not in readme


def test_android_ci_runs_tests_lint_and_build_through_the_verified_wrapper() -> None:
    workflow = Path(".github/workflows/build-android.yml").read_text(encoding="utf-8")

    assert "actions/setup-java@" in workflow
    assert "java-version: '17'" in workflow
    assert "498495120a03b9a6ab5d155f5de3c8f0d986a449153702fb80fc80e134484f17" in workflow
    assert "sha256sum --check --strict" in workflow
    assert "./mobile/ElrenMobile/gradlew -p mobile/ElrenMobile" in workflow
    assert ":app:testDebugUnitTest" in workflow
    assert ":app:lintDebug" in workflow
    assert ":app:assembleDebug" in workflow
    assert "persist-credentials: false" in workflow


def test_android_activity_survives_foreground_service_restrictions() -> None:
    source = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/MainActivity.kt"
    ).read_text(encoding="utf-8")
    store = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/SecureStore.kt"
    ).read_text(encoding="utf-8")

    assert "startConnectionServiceSafely()" in source
    assert "catch (error: Throwable)" in source
    assert "立即连接电脑" in source
    assert "postDelayed({ startConnectionServiceSafely() }, 900)" in source
    assert "connectionError()" in store
    assert "clearConnectionError()" in store
    connect_if_paired = source.split("private fun connectIfPaired()", 1)[1].split(
        "private fun startConnectionServiceSafely()", 1
    )[0]
    assert 'store.connectionState() == "online"' not in connect_if_paired
    assert "startConnectionServiceSafely()" in connect_if_paired
    assert 'savedEndpoint.startsWith("ws://"' in store
    assert 'savedEndpoint.startsWith("wss://"' in store
    assert "fun isPaired(): Boolean" in store
    assert "!store.isPaired()" in source


def test_android_pairing_http_calls_are_bounded_and_validate_the_qr_endpoint() -> None:
    client = Path(
        "mobile/ElrenMobile/app/src/main/java/ai/elren/mobile/PairingClient.kt"
    ).read_text(encoding="utf-8")

    assert ".callTimeout(15, TimeUnit.SECONDS)" in client
    assert ".connectTimeout(8, TimeUnit.SECONDS)" in client
    assert "toHttpUrlOrNull()" in client
    assert "endpointUrl.username.isEmpty()" in client
    assert "endpointUrl.query == null" in client
    assert 'pairingId.matches(Regex("[a-fA-F0-9]{32}"))' in client
    assert "callback(runCatching" in client
    assert "store.save(endpoint, deviceId, key)" in client


def test_android_build_does_not_publish_machine_local_gradle_state() -> None:
    ignores = Path("mobile/ElrenMobile/.gitignore").read_text(encoding="utf-8")
    assert "local.properties" in ignores
    assert "app/build/" in ignores
    assert ".gradle/" in ignores
