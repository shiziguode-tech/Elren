package ai.elren.mobile

/** One input transaction; never enables an IME or overrides a user's later selection. */
internal class TemporaryInputMethod(private val port: Port) {
    interface Port {
        fun selected(): String
        fun switchTo(id: String): Boolean
        fun ready(): Boolean
        fun pause()
    }

    fun <T> use(target: String, input: () -> T): T {
        val previous = port.selected()
        check(previous.isNotBlank()) { "Cannot determine current keyboard; no input attempted" }
        val switching = previous != target
        try {
            if (switching) check(port.switchTo(target)) {
                "Keyboard switch rejected; enable Elren input in system settings first"
            }
            var ready = false
            for (attempt in 0 until 40) {
                val current = port.selected()
                check(current == previous || current == target) { "Keyboard changed by user; input cancelled" }
                if (current == target && port.ready()) { ready = true; break }
                port.pause()
            }
            check(ready) { "Keyboard/editor not ready; no input attempted" }
            return input()
        } finally {
            // Never retry text entry on a failed transaction. Restore only our
            // own selection; an explicit user switch to another IME wins.
            if (switching && port.selected() == target) port.switchTo(previous)
        }
    }
}
