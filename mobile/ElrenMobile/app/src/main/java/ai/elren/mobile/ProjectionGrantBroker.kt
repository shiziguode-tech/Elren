package ai.elren.mobile

import android.content.Intent
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/** Holds the exact MediaProjection grant until the foreground service consumes it. */
internal object ProjectionGrantBroker {
    data class Grant(val resultCode: Int, val data: Intent)

    private val grants = ConcurrentHashMap<String, Grant>()

    fun stage(resultCode: Int, data: Intent): String {
        grants.clear()
        val requestId = UUID.randomUUID().toString()
        grants[requestId] = Grant(resultCode, data)
        return requestId
    }

    fun consume(requestId: String): Grant? = grants.remove(requestId)

    fun has(requestId: String): Boolean =
        requestId.isNotBlank() && grants.containsKey(requestId)

    fun discard(requestId: String) {
        grants.remove(requestId)
    }
}
