package ai.elren.mobile

/** A traversal failure is unknown UI state, never evidence of authentication. */
internal object AuthenticationScreenScan {
    enum class Result { CLEAR, AUTHENTICATION, UNKNOWN }

    fun <T : Any> scan(root: T?, children: (T) -> List<T>, protected: (T) -> Boolean,
                      limit: Int = 10_000): Result {
        if (root == null) return Result.UNKNOWN
        val pending = java.util.ArrayDeque<T>()
        pending.add(root)
        var seen = 0
        while (pending.isNotEmpty()) {
            if (seen++ >= limit) return Result.UNKNOWN
            val node = pending.removeFirst()
            if (protected(node)) return Result.AUTHENTICATION
            val next = children(node)
            if (seen + pending.size + next.size > limit) return Result.UNKNOWN
            pending.addAll(next)
        }
        return Result.CLEAR
    }

    fun isAuthenticationField(visible: Boolean, password: Boolean, editable: Boolean,
                              sensitiveMarker: Boolean): Boolean =
        visible && (password || (editable && sensitiveMarker))
}
