package ai.elren.mobile

import android.util.Base64
import org.json.JSONObject
import java.nio.charset.StandardCharsets
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

object CryptoProtocol {
    private val aad = "elren-mobile-v1".toByteArray(StandardCharsets.UTF_8)
    private val random = SecureRandom()

    fun b64(value: ByteArray): String = Base64.encodeToString(value, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
    fun unb64(value: String): ByteArray = Base64.decode(value, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)

    private fun hmac(key: ByteArray, text: String): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return mac.doFinal(text.toByteArray(StandardCharsets.UTF_8))
    }

    fun deriveDeviceKey(secret: ByteArray, pairingId: String, deviceId: String): ByteArray =
        hmac(secret, "elren-mobile-device\u0000$pairingId\u0000$deviceId")

    fun pairingProof(secret: ByteArray, pairingId: String, deviceId: String, nonce: String): String =
        b64(hmac(secret, "pair\u0000$pairingId\u0000$deviceId\u0000$nonce"))

    fun connectionSignature(key: ByteArray, deviceId: String, timestamp: Long, nonce: String): String =
        b64(hmac(key, "connect\u0000$deviceId\u0000$timestamp\u0000$nonce"))

    fun revocationSignature(key: ByteArray, deviceId: String, timestamp: Long, nonce: String): String =
        b64(hmac(key, "revoke\u0000$deviceId\u0000$timestamp\u0000$nonce"))

    fun encrypt(key: ByteArray, payload: JSONObject): JSONObject {
        val nonce = ByteArray(12).also(random::nextBytes)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        cipher.updateAAD(aad)
        val output = cipher.doFinal(payload.toString().toByteArray(StandardCharsets.UTF_8))
        val ciphertext = output.copyOfRange(0, output.size - 16)
        val tag = output.copyOfRange(output.size - 16, output.size)
        return JSONObject()
            .put("v", 1)
            .put("nonce", b64(nonce))
            .put("ciphertext", b64(ciphertext))
            .put("tag", b64(tag))
    }

    fun decrypt(key: ByteArray, envelope: JSONObject): JSONObject {
        require(envelope.optInt("v") == 1) { "Unsupported protocol" }
        val nonce = unb64(envelope.getString("nonce"))
        val ciphertext = unb64(envelope.getString("ciphertext"))
        val tag = unb64(envelope.getString("tag"))
        require(nonce.size == 12 && tag.size == 16 && ciphertext.size <= 16 * 1024 * 1024) { "Invalid packet" }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        cipher.updateAAD(aad)
        val plain = cipher.doFinal(ciphertext + tag)
        return JSONObject(String(plain, StandardCharsets.UTF_8))
    }
}
