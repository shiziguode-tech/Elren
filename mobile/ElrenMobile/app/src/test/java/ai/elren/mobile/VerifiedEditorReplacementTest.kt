package ai.elren.mobile

import org.junit.Assert.*
import org.junit.Test

class VerifiedEditorReplacementTest {
    private class Editor(var text: String = "草稿🙂") : VerifiedEditorReplacement.Connection {
        var offset = 0
        var start = text.length
        var end = text.length
        var acceptsSelection = true
        var commits = 0
        override fun snapshot() = VerifiedEditorReplacement.Snapshot(text, offset, start, end)
        override fun selectAll(length: Int) {
            if (acceptsSelection) { start = 0; end = length }
        }
        override fun commit(text: String) {
            commits++
            this.text = this.text.substring(0, start) + text + this.text.substring(end)
        }
    }

    @Test fun replacesFullUnicodeDraft() {
        val editor = Editor()
        assertTrue(VerifiedEditorReplacement {}.replace(editor, "新文字", editor.text))
        assertEquals("新文字", editor.text)
    }

    @Test fun rejectedSelectionDoesNotAppend() {
        val editor = Editor().apply { acceptsSelection = false }
        assertFalse(VerifiedEditorReplacement {}.replace(editor, "new", editor.text))
        assertEquals(0, editor.commits)
        assertEquals("草稿🙂", editor.text)
    }

    @Test fun delayedSelectionIsAwaited() {
        val editor = Editor().apply { acceptsSelection = false }
        var ticks = 0
        val replacement = VerifiedEditorReplacement {
            if (++ticks == 3) { editor.start = 0; editor.end = editor.text.length }
        }
        assertTrue(replacement.replace(editor, "new", editor.text))
        assertEquals("new", editor.text)
    }

    @Test fun truncatedSurroundingTextIsRejected() {
        val editor = Editor().apply { offset = 12 }
        assertFalse(VerifiedEditorReplacement {}.replace(editor, "new", editor.text))
        assertEquals(0, editor.commits)
    }

    @Test fun editWhileAwaitingSelectionIsNotOverwritten() {
        val editor = Editor()
        val replacement = VerifiedEditorReplacement { editor.text = "user change" }
        assertFalse(replacement.replace(editor, "new", editor.text))
        assertEquals(0, editor.commits)
    }

    @Test fun staleExpectedValueIsRejected() {
        val editor = Editor()
        assertFalse(VerifiedEditorReplacement {}.replace(editor, "new", "wrong draft"))
        assertEquals(0, editor.commits)
    }

    @Test fun emptyFieldCanReceiveText() {
        val editor = Editor("")
        assertTrue(VerifiedEditorReplacement {}.replace(editor, "中文", ""))
        assertEquals("中文", editor.text)
    }
}
