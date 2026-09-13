from __future__ import annotations

import re

READ_ONLY_PREFIXES = (
    "get-childitem", "dir", "ls", "rg", "select-string", "get-content", "type ",
    "git status", "git diff", "git log", "python --version", "node --version",
    "get-process", "get-location", "pwd", "where.exe", "get-command",
)
DANGEROUS_PATTERNS = re.compile(
    r"(?i)(remove-item|\brm\b|del\s|erase\s|format\s|shutdown|restart-computer|"
    r"stop-process|taskkill|reg\s+(delete|add)|set-executionpolicy|invoke-expression|\biex\b|"
    r"curl.+\|\s*(?:sh|bash)|iwr.+\|\s*iex)"
)
SENSITIVE_PATTERNS = re.compile(
    r"(?i)(\.env(?:\s|$)|deepseek_(?:backup_)?api_key|"
    r"provider-(?:keys\.json|secrets\.vault)|gateway\.token|openclaw\.json|"
    r"feishu-listener-token(?:\.vault)?|mobile-device-secrets\.vault|mobile-devices\.json|"
    r"\bkey_local\b|"
    r"deepdesk\.db(?:-wal|-shm)?|audit\.jsonl|"
    r"elren_accept_control_file_changes|"
    r"(?:get-childitem|gci|dir)\s+env:|\.ssh|\.aws|\.azure|credentials)"
)

# An Agent never needs OS-level persistence to implement Elren schedules. The
# host scheduler has its own bounded/audited API. Blocking these launch-survival
# primitives also prevents a child from waiting until the control guard releases
# its lease and then accepting a forged baseline.
OS_PERSISTENCE_PATTERNS = re.compile(
    r"(?i)(\bschtasks(?:\.exe)?\b|\bat(?:\.exe)?\s|"
    r"\b(?:register|new|set)-scheduledtask\b|"
    r"\bsc(?:\.exe)?\s+(?:create|config)\b|\bnew-service\b|"
    r"\\currentversion\\run(?:once)?\b|\\startup\\|"
    r"\bwmic\b.+\bprocess\b.+\bcall\b.+\bcreate\b|"
    r"\b(?:crontab|systemctl\s+(?:enable|edit)|launchctl\s+load)\b)"
)

# Free-form shells must not terminate the Python/Elren host that owns them.
# The process-manager tool already offers exact-PID termination while
# protecting the host and its parent chain, so shell process termination is
# always routed there.
SHELL_PROCESS_TERMINATION_PATTERNS = re.compile(
    r"(?i)(\bstop-process\b|\btaskkill(?:\.exe)?\b|\b(?:pkill|killall)\b|"
    r"(?:^|[;&|\s])kill(?:\s|$)|\.\s*(?:kill|terminate)\s*\(\s*\))"
)


def binds_reserved_service_port(command: str, ports: set[int] | None = None) -> bool:
    """Detect attempts to start a preview server on Elren's own loopback port."""

    value = str(command or "")
    for port in ports or {8765}:
        token = re.escape(str(int(port)))
        if re.search(
            rf"(?ix)("
            rf"\bhttp\.server\s+{token}\b|"
            rf"\b(?:uvicorn|hypercorn|waitress-serve|flask)\b[^\r\n;&|]*"
            rf"(?:--port(?:=|\s+)|-p\s+){token}\b|"
            rf"\b(?:listen|bind)\s*\(\s*(?:['\"](?:127\.0\.0\.1|localhost)['\"]\s*,\s*)?{token}\b"
            rf")",
            value,
        ):
            return True
    return False

# Reading every environment variable can expose credentials, but rejecting every
# ``$env:...`` reference also blocks ordinary build/test commands that only need
# a temporary or runtime directory. Keep a deliberately small non-secret allowlist
# and reject unknown/sensitive names rather than treating the whole environment as
# confidential.
SAFE_ENVIRONMENT_NAMES = {
    "TEMP",
    "TMP",
    "TMPDIR",
    "SYSTEMROOT",
    "WINDIR",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "LOCALAPPDATA",
    "APPDATA",
    "USERPROFILE",
    "PUBLIC",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "PROCESSOR_ARCHITECTURE",
}
ENVIRONMENT_REFERENCE_PATTERN = re.compile(r"(?i)\$env:([A-Za-z_][A-Za-z0-9_()]*)")


def accesses_sensitive_environment(
    command: str,
    allowed_names: set[str] | None = None,
) -> bool:
    """Reject credential discovery while allowing ordinary runtime path use."""

    value = str(command or "")
    if SENSITIVE_PATTERNS.search(value):
        return True
    allowed = SAFE_ENVIRONMENT_NAMES | {
        str(name).upper() for name in (allowed_names or set())
    }
    return any(
        match.group(1).upper() not in allowed
        for match in ENVIRONMENT_REFERENCE_PATTERN.finditer(value)
    )

# Raw shell deletion is intentionally stricter than the general dangerous-command
# classifier.  Ordinary user wording such as "delete this file" means a recoverable
# move to the recycle bin through filesystem.delete.  Only unmistakable wording in
# the current user message authorizes an irreversible shell deletion.
SHELL_DELETE_PATTERNS = re.compile(
    r"(?i)(remove-item\b|(?:^|[;&|\s])rm(?:\s|$)|(?:^|[;&|\s])del(?:\s|$)|"
    r"(?:^|[;&|\s])erase(?:\s|$)|(?:^|[;&|\s])rmdir(?:\s|$)|"
    r"\[\s*(?:system\.)?io\.(?:file|directory)\s*\]\s*::\s*delete\s*\(|"
    r"(?:os\.)?(?:remove|unlink)\s*\(|(?:shutil\.)?rmtree\s*\(|"
    r"(?:pathlib\.)?path\s*\([^)]*\)\s*\.\s*unlink\s*\()"
)

PERMANENT_DELETE_REQUEST_PATTERNS = re.compile(
    r"(?i)(彻底删除|永久删除|不可恢复(?:地)?删除|跳过回收站|不要(?:放到|移到|进入)?回收站|"
    r"直接从磁盘删除|permanently\s+(?:delete|remove)|delete\s+permanently|"
    r"irreversibly\s+(?:delete|remove)|bypass\s+the\s+recycle\s+bin|"
    r"skip\s+the\s+recycle\s+bin|do\s+not\s+(?:use|move\s+to)\s+the\s+recycle\s+bin)"
)


def allows_permanent_delete(user_prompt: str) -> bool:
    """Return true only when the current user message explicitly requests it."""

    return bool(PERMANENT_DELETE_REQUEST_PATTERNS.search(str(user_prompt or "")))
