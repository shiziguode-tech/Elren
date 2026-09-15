package ai.elren.mobile

/** Blocking worker-side transaction. Android operations are marshalled to the main thread by Target. */
internal class VerifiedTextInput(private val pause: (Long) -> Unit = Thread::sleep) {
    interface Target {
        // Must fail if the original field disappears, loses focus, or becomes protected.
        fun read(): String
        fun setText(text: String)
        fun replaceThroughInputConnection(text: String, expected: String): Boolean
    }

    fun replace(target: Target, text: String): String {
        require(text.length <= 20_000) { "Text is too long" }
        val original = target.read()
        if (original == text) {
            check(awaitText(target, text)) { "Input field changed; inspect the screen before retrying" }
            return "already_present"
        }
        target.setText(text)
        if (awaitText(target, text)) return "accessibility_set_text"
        // A user edit or a partial write is not permission to replace a different value.
        check(target.read() == original) {
            "Input field changed unexpectedly; inspect the screen before retrying"
        }
        if (!target.replaceThroughInputConnection(text, original)) {
            error("Text was not entered. Tap the intended input field to activate the keyboard, then retry")
        }
        check(awaitText(target, text)) {
            "Android accepted the input request, but the text could not be verified; inspect the screen before retrying"
        }
        return "accessibility_input_connection"
    }

    private fun awaitText(target: Target, text: String): Boolean {
        // Require two matching observations so a transient SET_TEXT echo is not success.
        var matches = 0
        repeat(10) {
            pause(100)
            matches = if (target.read() == text) matches + 1 else 0
            if (matches >= 2) return true
        }
        return false
    }
}

/** Select the full, observed value and confirm the selection before committing a replacement. */
internal class VerifiedEditorReplacement(private val pause: (Long) -> Unit = Thread::sleep) {
    data class Snapshot(val text: String, val offset: Int, val start: Int, val end: Int)
    interface Connection {
        fun snapshot(): Snapshot?
        fun selectAll(length: Int)
        fun commit(text: String)
    }

    fun replace(connection: Connection, text: String, expected: String): Boolean {
        fun unchanged(snapshot: Snapshot?) = snapshot != null && snapshot.offset == 0 && snapshot.text == expected
        if (!unchanged(connection.snapshot())) return false
        connection.selectAll(expected.length)
        repeat(8) {
            pause(60)
            val snapshot = connection.snapshot()
            if (!unchanged(snapshot)) return false
            if (snapshot!!.start == 0 && snapshot.end == expected.length) {
                connection.commit(text)
                return true
            }
        }
        return false
    }
}
