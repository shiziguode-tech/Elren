package ai.elren.mobile

import org.junit.Assert.*
import org.junit.Test

class TemporaryInputMethodTest {
    private class Fake : TemporaryInputMethod.Port {
        var current = "user"
        var accepted = true
        var connected = true
        var pauses = 0
        val switches = mutableListOf<String>()
        override fun selected() = current
        override fun switchTo(id: String): Boolean {
            switches.add(id)
            if (accepted) current = id
            return accepted
        }
        override fun ready() = connected
        override fun pause() { pauses++ }
    }
    @Test fun switchesOnlyForTransactionAndRestores() {
        val p = Fake()
        assertEquals("done", TemporaryInputMethod(p).use("elren") {
            assertEquals("elren", p.current); "done"
        })
        assertEquals(listOf("elren", "user"), p.switches)
    }
    @Test fun failureRestoresWithoutRetryingInput() {
        val p = Fake(); var calls = 0
        assertThrows(IllegalArgumentException::class.java) {
            TemporaryInputMethod(p).use("elren") { calls++; throw IllegalArgumentException("failed") }
        }
        assertEquals(1, calls); assertEquals("user", p.current)
    }
    @Test fun manuallySelectedElrenStaysSelected() {
        val p = Fake(); p.current = "elren"
        TemporaryInputMethod(p).use("elren") { }
        assertTrue(p.switches.isEmpty())
    }
    @Test fun userKeyboardSelectionWins() {
        val p = Fake()
        TemporaryInputMethod(p).use("elren") { p.current = "other" }
        assertEquals("other", p.current); assertEquals(listOf("elren"), p.switches)
    }
    @Test fun rejectedSwitchNeverWrites() {
        val p = Fake(); p.accepted = false
        assertThrows(IllegalStateException::class.java) {
            TemporaryInputMethod(p).use("elren") { fail("must not write") }
        }
        assertEquals("user", p.current)
    }
    @Test fun readinessTimeoutRestoresWithoutWriting() {
        val p = Fake(); p.connected = false
        assertThrows(IllegalStateException::class.java) {
            TemporaryInputMethod(p).use("elren") { fail("must not write") }
        }
        assertEquals(40, p.pauses); assertEquals("user", p.current)
    }
    @Test fun unknownOriginalKeyboardNeverSwitches() {
        val p = Fake(); p.current = ""
        assertThrows(IllegalStateException::class.java) {
            TemporaryInputMethod(p).use("elren") { fail("must not write") }
        }
        assertTrue(p.switches.isEmpty())
    }
}
