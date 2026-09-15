package ai.elren.mobile

import org.junit.Assert.*
import org.junit.Test

class VerifiedTextInputTest {
    private class Field(var value: String = "") : VerifiedTextInput.Target {
        var direct: (String) -> Unit = { value = it }
        var fallback: (String, String) -> Boolean = { text, expected ->
            check(value == expected); value = text; true
        }
        var valid = true
        var writes = 0
        var fallbacks = 0
        override fun read(): String { check(valid) { "Focus changed" }; return value }
        override fun setText(text: String) { writes++; direct(text) }
        override fun replaceThroughInputConnection(text: String, expected: String): Boolean {
            fallbacks++; return fallback(text, expected)
        }
    }

    @Test fun unicodeAndEmojiRoundTrip() {
        val field = Field()
        assertEquals("accessibility_set_text", VerifiedTextInput {}.replace(field, "微信输入测试🙂\n第二行"))
        assertEquals(0, field.fallbacks)
    }

    @Test fun acceptedButEmptyUsesVerifiedFallback() {
        val field = Field().apply { direct = {} }
        assertEquals("accessibility_input_connection", VerifiedTextInput {}.replace(field, "中文"))
        assertEquals("中文", field.value)
        assertEquals(1, field.fallbacks)
    }

    @Test fun fallbackReplacesRatherThanAppends() {
        val field = Field("old draft").apply { direct = {} }
        VerifiedTextInput {}.replace(field, "新草稿")
        assertEquals("新草稿", field.value)
    }

    @Test fun acceptedButUnchangedIsAnError() {
        val field = Field().apply { direct = {}; fallback = { _, _ -> true } }
        assertThrows(IllegalStateException::class.java) { VerifiedTextInput {}.replace(field, "test") }
        assertEquals(1, field.fallbacks)
    }

    @Test fun missingInputConnectionIsAnError() {
        val field = Field().apply { direct = {}; fallback = { _, _ -> false } }
        assertThrows(IllegalStateException::class.java) { VerifiedTextInput {}.replace(field, "test") }
    }

    @Test fun delayedDirectWriteDoesNotDuplicate() {
        val field = Field().apply { direct = {} }
        var ticks = 0
        val input = VerifiedTextInput { if (++ticks == 3) field.value = "中文" }
        assertEquals("accessibility_set_text", input.replace(field, "中文"))
        assertEquals(0, field.fallbacks)
    }

    @Test fun transientEchoDoesNotReportSuccess() {
        val field = Field().apply { direct = {} }
        var ticks = 0
        val input = VerifiedTextInput {
            ticks++
            if (ticks == 1) field.value = "中文"
            if (ticks == 2) field.value = ""
        }
        assertEquals("accessibility_input_connection", input.replace(field, "中文"))
    }

    @Test fun userEditStopsFallback() {
        val field = Field().apply { direct = { value = "user edit" } }
        assertThrows(IllegalStateException::class.java) { VerifiedTextInput {}.replace(field, "test") }
        assertEquals(0, field.fallbacks)
        assertEquals("user edit", field.value)
    }

    @Test fun focusLossStopsFallback() {
        val field = Field().apply { direct = { valid = false } }
        assertThrows(IllegalStateException::class.java) { VerifiedTextInput {}.replace(field, "test") }
        assertEquals(0, field.fallbacks)
    }

    @Test fun repeatedCommandDoesNotWriteAgain() {
        val field = Field("already entered")
        assertEquals("already_present", VerifiedTextInput {}.replace(field, "already entered"))
        assertEquals(0, field.writes)
    }

    @Test fun emptyTextClearsExistingDraft() {
        val field = Field("draft")
        VerifiedTextInput {}.replace(field, "")
        assertEquals("", field.value)
    }

    @Test fun oversizedTextNeverWrites() {
        val field = Field()
        assertThrows(IllegalArgumentException::class.java) { VerifiedTextInput {}.replace(field, "x".repeat(20_001)) }
        assertEquals(0, field.writes)
    }
}
