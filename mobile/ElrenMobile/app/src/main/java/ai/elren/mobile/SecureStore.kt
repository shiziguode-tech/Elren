package ai.elren.mobile

import android.annotation.SuppressLint
import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class PairedComputer(val endpoint: String, val deviceId: String, val key: ByteArray)

class SecureStore(private val context: Context) {
    private val prefs = context.getSharedPreferences("elren_mobile", Context.MODE_PRIVATE)
    private val alias = "elren-mobile-device-key"

    fun deviceId(): String {
        prefs.getString("device_id", null)?.let { return it }
        // This identifier only scopes a pairing. A random, app-local value is
        // sufficient and avoids turning a stable Android hardware identifier
        // into application data.
        val value = "android-${UUID.randomUUID()}"
        prefs.edit().putString("device_id", value).apply()
        return value
    }

    private fun masterKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build())
            generateKey()
        }
    }

    fun save(endpoint: String, deviceId: String, key: ByteArray) {
        require(key.size == 32) { "The pairing key must be 256 bits" }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, masterKey())
        val encrypted = cipher.doFinal(key)
        val saved = prefs.edit()
            .putString("endpoint", endpoint)
            .putString("paired_device_id", deviceId)
            .putString("key_iv", CryptoProtocol.b64(cipher.iv))
            .putString("key_data", CryptoProtocol.b64(encrypted))
            .putString("connection_state", "connecting")
            .remove("connection_error")
            .commit()
        check(saved) { "The encrypted pairing could not be saved on this phone" }
    }

    fun load(): PairedComputer? = try {
        val savedEndpoint = prefs.getString("endpoint", null) ?: return null
        // Early companion builds stored a WebSocket URL. The bridge endpoint
        // itself is HTTP(S); migrate those persisted pairings in place so an
        // app update does not force the user to scan a new code.
        val endpoint = when {
            savedEndpoint.startsWith("ws://", ignoreCase = true) ->
                "http://${savedEndpoint.substring(5)}"
            savedEndpoint.startsWith("wss://", ignoreCase = true) ->
                "https://${savedEndpoint.substring(6)}"
            else -> savedEndpoint
        }.trimEnd('/')
        if (endpoint != savedEndpoint) {
            prefs.edit().putString("endpoint", endpoint).apply()
        }
        val deviceId = prefs.getString("paired_device_id", null) ?: return null
        val iv = CryptoProtocol.unb64(prefs.getString("key_iv", null) ?: return null)
        val encrypted = CryptoProtocol.unb64(prefs.getString("key_data", null) ?: return null)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, masterKey(), GCMParameterSpec(128, iv))
        val key = cipher.doFinal(encrypted)
        require(key.size == 32) { "The stored pairing key is invalid" }
        PairedComputer(endpoint, deviceId, key)
    } catch (_: Exception) { null }

    /**
     * Cheap UI/lifecycle check that avoids opening AndroidKeyStore every 750ms.
     * The foreground service still decrypts and validates the complete record
     * before it makes a connection.
     */
    fun isPaired(): Boolean =
        !prefs.getString("endpoint", null).isNullOrBlank() &&
            !prefs.getString("paired_device_id", null).isNullOrBlank() &&
            !prefs.getString("key_iv", null).isNullOrBlank() &&
            !prefs.getString("key_data", null).isNullOrBlank()

    fun clear() {
        prefs.edit()
            .remove("endpoint")
            .remove("paired_device_id")
            .remove("key_iv")
            .remove("key_data")
            .remove("connection_state")
            .remove("connection_error")
            .remove("screen_capture_active")
            .remove("screen_capture_error")
            .remove("screen_capture_phase")
            .remove("screen_capture_owner")
            .remove("screen_capture_updated_at")
            .apply()
    }

    fun connectionState(): String = prefs.getString("connection_state", "offline") ?: "offline"
    fun setConnectionState(value: String) = prefs.edit().putString("connection_state", value).apply()

    fun connectionError(): String = prefs.getString("connection_error", "").orEmpty()
    fun setConnectionError(value: String) = prefs.edit()
        .putString("connection_error", value.take(240))
        .apply()
    fun clearConnectionError() = prefs.edit().remove("connection_error").apply()

    fun screenCaptureActive(): Boolean = prefs.getBoolean("screen_capture_active", false)
    fun screenCaptureError(): String = prefs.getString("screen_capture_error", "").orEmpty()
    fun screenCapturePhase(): String = prefs.getString("screen_capture_phase", "idle")
        ?.takeIf { it in setOf("idle", "grant_received", "starting", "active", "expired", "error") }
        ?: "idle"
    fun screenCaptureOwner(): String = prefs.getString("screen_capture_owner", "").orEmpty()
    fun screenCaptureUpdatedAt(): Long = prefs.getLong("screen_capture_updated_at", 0L)

    @SuppressLint("ApplySharedPref")
    fun setScreenCaptureState(
        active: Boolean,
        error: String = "",
        phase: String? = null,
        ownerId: String? = null,
    ) {
        val resolvedPhase = phase ?: when {
            active -> "active"
            error.isNotBlank() -> "error"
            else -> "idle"
        }
        require(resolvedPhase in setOf("idle", "grant_received", "starting", "active", "expired", "error")) {
            "Unsupported screen-capture phase"
        }
        val editor = prefs.edit()
            .putBoolean("screen_capture_active", active)
            .putString("screen_capture_phase", resolvedPhase)
            .putLong("screen_capture_updated_at", System.currentTimeMillis())
        if (ownerId != null) {
            if (ownerId.isBlank()) editor.remove("screen_capture_owner")
            else editor.putString("screen_capture_owner", ownerId.take(80))
        } else if (resolvedPhase == "idle") {
            editor.remove("screen_capture_owner")
        }
        if (error.isBlank()) editor.remove("screen_capture_error")
        else editor.putString("screen_capture_error", error.take(240))
        // This state is consumed immediately by the foreground Activity after
        // a MediaProjection result. Commit synchronously so a slow/OEM service
        // startup cannot leave the UI reading the previous value.
        editor.commit()
    }

    fun autonomousEnabled(): Boolean = prefs.getBoolean("autonomous", false)
    fun setAutonomousEnabled(value: Boolean) = prefs.edit().putBoolean("autonomous", value).apply()

    fun interfaceLanguage(): String = prefs.getString("interface_language", "system")
        ?.takeIf { it in setOf("system", "zh", "en") }
        ?: "system"

    fun setInterfaceLanguage(value: String) {
        require(value in setOf("system", "zh", "en")) { "Unsupported interface language" }
        prefs.edit().putString("interface_language", value).apply()
    }

    fun usesChinese(systemLanguage: String): Boolean = when (interfaceLanguage()) {
        "zh" -> true
        "en" -> false
        else -> systemLanguage.equals("zh", ignoreCase = true)
    }
}
