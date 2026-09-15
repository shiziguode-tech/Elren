package ai.elren.mobile

import org.junit.Assert.*
import org.junit.Test

class EditorSnapshotValidationTest {
    @Test fun fullUnicodeDraftIsAccepted() {
        val value = "你好🙂"
        assertEquals(value, EditorSnapshotValidation.complete(value, 0, -1, -1, 0, value.length)?.text)
    }
    @Test fun emptyFieldIsValid() {
        assertNotNull(EditorSnapshotValidation.complete("", 0, -1, -1, 0, 0))
    }
    @Test fun missingReadIsNotAnEmptyField() {
        assertNull(EditorSnapshotValidation.complete(null, 0, -1, -1, 0, 0))
    }
    @Test fun partialUpdatesCannotReplaceDrafts() {
        assertNull(EditorSnapshotValidation.complete("a", 0, 0, 1, 0, 1))
        assertNull(EditorSnapshotValidation.complete("a", 0, 1, 1, 0, 1))
    }
    @Test fun fullTextIgnoresUndefinedPartialEndOffset() {
        assertNotNull(EditorSnapshotValidation.complete("a", 0, -1, 0, 0, 1))
    }
    @Test fun truncatedPrefixCannotReplaceDrafts() {
        assertNull(EditorSnapshotValidation.complete("a", 12, -1, -1, 0, 1))
    }
    @Test fun invalidSelectionCannotBeCommitted() {
        assertNull(EditorSnapshotValidation.complete("a", 0, -1, -1, -1, 1))
        assertNull(EditorSnapshotValidation.complete("a", 0, -1, -1, 0, 2))
    }
    @Test fun oversizedDraftIsRejected() {
        assertNull(EditorSnapshotValidation.complete("a".repeat(20_001), 0, -1, -1, 0, 0))
    }
}
