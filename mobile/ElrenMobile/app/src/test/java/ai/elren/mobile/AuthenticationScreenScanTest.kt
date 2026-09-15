package ai.elren.mobile

import org.junit.Assert.*
import org.junit.Test

class AuthenticationScreenScanTest {
    private data class Node(val secret: Boolean = false, val children: List<Node> = emptyList())
    private fun scan(n: Node?, limit: Int = 10_000) =
        AuthenticationScreenScan.scan(n, { it.children }, { it.secret }, limit)

    @Test fun deepLauncherIsNotAuthentication() {
        var root = Node()
        repeat(100) { root = Node(children = listOf(root)) }
        assertEquals(AuthenticationScreenScan.Result.CLEAR, scan(root))
    }
    @Test fun deepPasswordStillBlocked() {
        var root = Node(secret = true)
        repeat(100) { root = Node(children = listOf(root)) }
        assertEquals(AuthenticationScreenScan.Result.AUTHENTICATION, scan(root))
    }
    @Test fun missingRootIsUnknown() {
        assertEquals(AuthenticationScreenScan.Result.UNKNOWN, scan(null))
    }
    @Test fun oversizedTreeIsUnknownRatherThanLocked() {
        assertEquals(AuthenticationScreenScan.Result.UNKNOWN,
            scan(Node(children = List(12) { Node() }), 10))
    }
    @Test fun invisiblePasswordDoesNotBlockCurrentScreen() {
        assertFalse(AuthenticationScreenScan.isAuthenticationField(false, true, true, true))
    }
    @Test fun launcherIconOrChatDescriptionIsNotCredentialEntry() {
        assertFalse(AuthenticationScreenScan.isAuthenticationField(true, false, false, true))
    }
    @Test fun visiblePasswordsRemainProtected() {
        assertTrue(AuthenticationScreenScan.isAuthenticationField(true, true, false, false))
    }
    @Test fun visibleOtpEntryRemainsProtected() {
        assertTrue(AuthenticationScreenScan.isAuthenticationField(true, false, true, true))
    }
}
