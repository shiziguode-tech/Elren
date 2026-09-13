package ai.elren.mobile

import android.content.Context
import android.net.Uri
import android.os.Build
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONObject
import java.io.IOException
import java.security.SecureRandom
import java.util.concurrent.TimeUnit

class PairingClient(private val context: Context) {
    // A missing/offline desktop must not leave the pairing screen spinning
    // indefinitely. WebSocket reconnects are handled by the foreground
    // service; these one-shot HTTP operations have a bounded lifetime.
    private val http = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
        .writeTimeout(12, TimeUnit.SECONDS)
        .callTimeout(15, TimeUnit.SECONDS)
        .build()
    private val store = SecureStore(context)

    fun pair(qrValue: String, callback: (Result<PairedComputer>) -> Unit) {
        try {
            val uri = Uri.parse(qrValue)
            require(uri.scheme == "elren" && uri.host == "pair") { "This is not an Elren pairing code" }
            require(uri.getQueryParameter("v") == "1") { "Unsupported pairing version" }
            val endpointValue = uri.getQueryParameter("endpoint")?.trim() ?: error("Missing endpoint")
            val endpointUrl = endpointValue.toHttpUrlOrNull() ?: error("Invalid computer endpoint")
            require(endpointUrl.scheme == "http" || endpointUrl.scheme == "https") {
                "The computer endpoint must use HTTP or HTTPS"
            }
            require(endpointUrl.username.isEmpty() && endpointUrl.password.isEmpty()) {
                "The computer endpoint must not contain credentials"
            }
            require(endpointUrl.query == null && endpointUrl.fragment == null) {
                "The computer endpoint must not contain a query or fragment"
            }
            val endpoint = endpointUrl.newBuilder()
                .encodedPath(endpointUrl.encodedPath.trimEnd('/').ifEmpty { "/" })
                .build()
                .toString()
                .trimEnd('/')
            val pairingId = uri.getQueryParameter("id") ?: error("Missing pairing ID")
            require(pairingId.matches(Regex("[a-fA-F0-9]{32}"))) { "Invalid pairing ID" }
            val secret = CryptoProtocol.unb64(uri.getQueryParameter("secret") ?: error("Missing pairing secret"))
            require(secret.size == 32) { "Invalid pairing secret" }
            val deviceId = store.deviceId()
            val nonceBytes = ByteArray(24).also(SecureRandom()::nextBytes)
            val nonce = CryptoProtocol.b64(nonceBytes)
            val key = CryptoProtocol.deriveDeviceKey(secret, pairingId, deviceId)
            val payload = JSONObject()
                .put("pairing_id", pairingId)
                .put("device_id", deviceId)
                .put("device_name", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
                .put("android_version", Build.VERSION.RELEASE)
                .put("app_version", BuildConfig.VERSION_NAME)
                .put("client_nonce", nonce)
                .put("proof", CryptoProtocol.pairingProof(secret, pairingId, deviceId, nonce))
            val request = Request.Builder()
                .url("$endpoint/pair")
                .post(payload.toString().toRequestBody("application/json".toMediaType()))
                .build()
            http.newCall(request).enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) = callback(Result.failure(e))
                override fun onResponse(call: Call, response: Response) {
                    response.use {
                        if (!it.isSuccessful) return callback(Result.failure(IOException("Pairing failed: HTTP ${it.code}")))
                        // AndroidKeyStore and durable preference writes can fail
                        // independently of the HTTP exchange. Always complete
                        // the callback so the pairing UI cannot spin forever.
                        callback(runCatching {
                            store.save(endpoint, deviceId, key)
                            PairedComputer(endpoint, deviceId, key)
                        })
                    }
                }
            })
        } catch (error: Exception) {
            callback(Result.failure(error))
        }
    }

    fun unpair(callback: (Result<Unit>) -> Unit) {
        val paired = store.load()
        if (paired == null) {
            store.clear()
            callback(Result.success(Unit))
            return
        }
        val timestamp = System.currentTimeMillis() / 1000
        val nonce = ByteArray(24).also(SecureRandom()::nextBytes).let(CryptoProtocol::b64)
        val signature = CryptoProtocol.revocationSignature(paired.key, paired.deviceId, timestamp, nonce)
        val url = "${paired.endpoint}/devices/${Uri.encode(paired.deviceId)}?ts=$timestamp&nonce=${Uri.encode(nonce)}&sig=${Uri.encode(signature)}"
        val request = Request.Builder().url(url).delete().build()
        http.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                store.clear()
                callback(Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                response.use {
                    if (!it.isSuccessful && it.code != 404) {
                        store.clear()
                        callback(Result.failure(IOException("Unpair failed: HTTP ${it.code}")))
                        return
                    }
                    store.clear()
                    callback(Result.success(Unit))
                }
            }
        })
    }
}
