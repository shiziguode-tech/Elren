from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".gradle",
    ".ignored",
    ".ignored_openclaw",
    ".kotlin",
    ".links",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
EXCLUDED_ROOT_DIRECTORIES = {"data", "dist", "outputs", "screenshots"}
EXCLUDED_FILE_NAMES = {".env", "local.properties", "skills-check.json"}
SENSITIVE_FILE_NAMES = {
    "deepdesk.db",
    "feishu-listener-token",
    "gateway.token",
    "openclaw.json",
    "provider-keys.json",
    "provider-secrets.vault",
    "runtime-settings.json",
}
ALLOWED_WORK_ENTRIES = {
    "browser-runtime",
    "node-runtime",
    "openclaw-runtime",
    "python-runtime",
    "runtime-bundle.json",
    "tool-runtime",
}
PRIVATE_CREDENTIAL_FILES = {
    (".env",),
    ("data", "provider-secrets.vault"),
    ("data", "runtime-settings.json"),
}
PRIVATE_HISTORY_FILES = {("data", "deepdesk.db")}
PRIVATE_DESKTOP_LANGUAGE_FILE = ("data", "desktop-ui-language.txt")
PRIVATE_PROFILE_NOTICE = "PRIVATE-DATA-NOTICE.txt"
PRIVATE_PROFILE_NOTICE_CONTENT = """Elren owner-private profile / Elren 个人私有资料包

HIGHLY SENSITIVE / 高度敏感

This owner-only package includes data/deepdesk.db, an unencrypted SQLite copy
of personal chats, tasks, in-app memories, and schedules. The provider vault is
Windows-protected, but that protection does not encrypt the history database.
Do not share this archive or extracted folder. Store and transfer it only in a
location protected by your operating-system account or full-disk encryption.

此仅限所有者使用的包包含 data/deepdesk.db，其中的聊天、任务、应用内记忆和
定时任务是未单独加密的 SQLite 数据。Windows 保护只适用于密钥保险库，并不
加密聊天数据库。请勿分享此压缩包或解压目录，仅在受系统账户或全盘加密保护的
位置保存和传输。
"""
PUBLIC_ALLOWED_ROOT_ENTRIES = {
    ".env.example",
    ".github",
    ".gitignore",
    ".venv",
    "agents.md",
    "benchmark-report.md",
    "build elren for macos.command",
    "deepdesk",
    "design-qa.md",
    "launcher",
    "live-computer-use-qa-report.json",
    "live-computer-use-qa-report.md",
    "macos",
    "microsoft.web.webview2.core.dll",
    "microsoft.web.webview2.winforms.dll",
    "mobile",
    "plugins",
    "pyproject.toml",
    "elren.exe",
    "elren.apk",
    "readme.md",
    "release-notes.md",
    "skills",
    "start.ps1",
    "tests",
    "third_party_notices.md",
    "webview2loader.dll",
    "work",
}
PUBLIC_VENV_ACTIVATION_FILES = {
    "activate",
    "activate.bat",
    "activate.fish",
    "activate.ps1",
    "deactivate.bat",
}
PORTABLE_VENV_PYTHON_EXECUTABLES = {
    "python.exe",
    "python3.exe",
    "pythonw.exe",
}
PUBLIC_ENV_TEMPLATE_NAME = ".env.example"
PROVIDER_VAULT_MAGIC = b"ELRENVLT\x01"
PUBLIC_ENV_CREDENTIAL_KEY = re.compile(
    r"(?:^|_)(?:api_?key|access_?key(?:_id)?|private_?key|password|passphrase|"
    r"secret(?:_key)?|client_?secret|token|bearer|cookie|credential|webhook(?:_url)?)$",
    re.IGNORECASE,
)
LEGACY_RUNTIME_PRIVATE_KEYS = {
    "aicodemirror",
    "aicodemirror_fable",
    "deepseek_backup",
    "deepseek_primary",
    "eightctl_email",
    "feishu_app_id",
    "feishu_open_id",
    "gemini",
    "huggingface",
    "model_providers",
    "pollinations",
    "spotify_client_id",
    "telegram_chat_id",
}
REPRODUCIBLE_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
EXCLUDED_NESTED_DIRECTORY_PATHS = {
    ("mobile", "elrenmobile", "build"),
    ("mobile", "elrenmobile", "app", "build"),
    ("work", "sandbox"),
    ("work", "studio"),
}
OPENCLAW_NODE_MODULES_PREFIX = ("work", "openclaw-runtime", "node_modules")
OPENCLAW_PNPM_STORE_PREFIX = OPENCLAW_NODE_MODULES_PREFIX + (".pnpm",)
PNPM_STORE_DIRECTORY_NAME = ".pnpm"
PNPM_WORKSPACE_STATE_NAME = ".pnpm-workspace-state-v1.json"
PNPM_MODULES_STATE_NAME = ".modules.yaml"
PYTHON_DEPENDENCY_STAMP = (".venv", ".elren-python-dependencies-v2")
NODE_DEPENDENCY_STAMP = (
    "work", "openclaw-runtime", "node_modules", ".elren-node-dependencies-v2"
)


def selected_private_files(
    *,
    include_private_keys: bool,
    include_private_history: bool,
) -> set[tuple[str, ...]]:
    selected: set[tuple[str, ...]] = set()
    if include_private_keys:
        selected.update(PRIVATE_CREDENTIAL_FILES)
    if include_private_history:
        selected.update(PRIVATE_HISTORY_FILES)
    if include_private_keys and include_private_history:
        # A narrowly scoped owner-profile preference, never public defaults or
        # a reason to allow any additional data-directory contents.
        selected.add(PRIVATE_DESKTOP_LANGUAGE_FILE)
    return selected


def read_private_desktop_language(path: Path) -> bytes:
    """Accept exactly one persisted UI token without copying arbitrary text."""
    with path.open("rb") as handle:
        language = handle.read(3)
    if language not in {b"en", b"zh"}:
        raise ValueError("data/desktop-ui-language.txt must contain exactly en or zh")
    return language


def is_openclaw_pnpm_store_path(parts: tuple[str, ...]) -> bool:
    """Return whether an archive path is inside pnpm's materialization store."""

    return (
        len(parts) >= len(OPENCLAW_PNPM_STORE_PREFIX)
        and parts[: len(OPENCLAW_PNPM_STORE_PREFIX)]
        == OPENCLAW_PNPM_STORE_PREFIX
    )


FIRST_PARTY_TEXT_SUFFIXES = {
    "",
    ".command",
    ".cs",
    ".css",
    ".html",
    ".js",
    ".json",
    ".kt",
    ".kts",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
FIRST_PARTY_TEXT_ROOTS = {
    ".github",
    "deepdesk",
    "launcher",
    "macos",
    "mobile",
    "plugins",
    "skills",
    "tests",
}
MACHINE_LOCAL_CLIPBOARD_REFERENCE = re.compile(
    r"(?i)(?:[a-z]:)?[\\/](?:users|documents and settings)[\\/]"
    r"[^\\/]+[\\/]appdata[\\/]local[\\/]temp[\\/]codex-clipboard-[^\s`\"']+"
)


def is_excluded_directory_name(name: str) -> bool:
    lowered = name.casefold()
    return lowered in EXCLUDED_DIRECTORY_NAMES or lowered.startswith(
        (".gradle-", ".kotlin-")
    )


def is_filesystem_link(path: Path) -> bool:
    """Recognize links, junctions, and unknown Windows reparse points."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    try:
        file_attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except FileNotFoundError:
        if os.name != "nt":
            raise
        get_file_attributes = ctypes.windll.kernel32.GetFileAttributesW  # type: ignore[attr-defined]
        get_file_attributes.argtypes = (ctypes.c_wchar_p,)
        get_file_attributes.restype = ctypes.c_uint32
        file_attributes = int(get_file_attributes(str(path)))
        if file_attributes == 0xFFFFFFFF:
            raise
    return bool(
        reparse_flag
        and file_attributes & reparse_flag
    )


def excluded(
    relative: Path,
    *,
    include_private_keys: bool = False,
    include_private_history: bool = False,
    allow_pnpm_store_source: bool = False,
) -> bool:
    lowered = tuple(part.lower() for part in relative.parts)
    parts = set(lowered)
    private_files = selected_private_files(
        include_private_keys=include_private_keys,
        include_private_history=include_private_history,
    )
    if lowered in private_files:
        return False
    if (
        lowered
        and lowered[0] not in PUBLIC_ALLOWED_ROOT_ENTRIES
        and lowered not in private_files
    ):
        # Both bundles use the product-root allowlist. The owner's archive may
        # additionally opt in to explicit credentials and/or history, but
        # unrelated notes and workspace drafts must never be swept into a
        # release merely because they sit beside the application.
        return True
    # Every archive must remain movable. Private-data opt-ins must never retain
    # build-machine activation scripts or editable-install absolute paths.
    if is_public_machine_local_venv_file(lowered):
        return True
    if parts & EXCLUDED_DIRECTORY_NAMES or any(
        is_excluded_directory_name(part) for part in lowered
    ):
        return True
    if lowered and lowered[0] in EXCLUDED_ROOT_DIRECTORIES:
        return True
    if (
        lowered
        and lowered[0] == "work"
        and len(lowered) > 1
        and lowered[1] not in ALLOWED_WORK_ENTRIES
    ):
        return True
    if lowered and (
        (
            lowered[-1].startswith(".env")
            and lowered[-1] != PUBLIC_ENV_TEMPLATE_NAME
        )
        or lowered[-1] in SENSITIVE_FILE_NAMES
    ):
        return True
    if lowered and lowered[-1] in EXCLUDED_FILE_NAMES:
        return True
    if any(lowered[: len(prefix)] == prefix for prefix in EXCLUDED_NESTED_DIRECTORY_PATHS):
        return True
    # pnpm's virtual store is only a source for the verified directory links
    # materialized by ``iter_release_paths``.  Shipping the store as well would
    # duplicate every reachable package in the archive.  ``.modules.yaml`` is
    # deliberately retained because the warm-start verifier reads its hoisted
    # linker marker; only the store tree itself is omitted.
    if PNPM_STORE_DIRECTORY_NAME in lowered and not allow_pnpm_store_source:
        return True
    if lowered[: len(OPENCLAW_NODE_MODULES_PREFIX)] == OPENCLAW_NODE_MODULES_PREFIX:
        nested_parts = lowered[len(OPENCLAW_NODE_MODULES_PREFIX) :]
        # Elren invokes OpenClaw's real .mjs entry directly and deliberately
        # never adds package-manager .bin folders to PATH. pnpm-generated
        # wrappers carry a build-machine target comment, while workspace state
        # records the absolute install root. Neither is runtime payload.
        if ".bin" in nested_parts or (
            nested_parts and nested_parts[-1] == PNPM_WORKSPACE_STATE_NAME
        ):
            return True
    # Runtime helpers (Chromium in particular) may leave debug.log beside an
    # otherwise immutable executable payload. Logs are machine-local state and
    # can contain paths or diagnostics, so neither public nor owner archives
    # may inherit them from a previously exercised package folder.
    # Debug databases are not runtime inputs.  Besides adding substantial
    # weight, upstream PDBs retain compiler-machine usernames and absolute
    # temporary paths, so they must not be redistributed in either archive.
    return relative.suffix.lower() in {".log", ".pdb", ".pyc"}


def excluded_directory_tree(
    relative: Path,
    *,
    include_private_keys: bool = False,
    include_private_history: bool = False,
) -> bool:
    """Return whether all descendants are guaranteed to be excluded.

    This is deliberately narrower than :func:`excluded`: exclusions that only
    apply to a particular file name cannot safely prune a same-named directory.
    Private archives also need to traverse the otherwise excluded ``data``
    directory to reach their explicitly allowlisted owner files.
    """

    lowered = tuple(part.lower() for part in relative.parts)
    if not lowered:
        return False
    private_files = selected_private_files(
        include_private_keys=include_private_keys,
        include_private_history=include_private_history,
    )
    if any(
        len(private_path) > len(lowered)
        and private_path[: len(lowered)] == lowered
        for private_path in private_files
    ):
        return False
    if lowered[0] not in PUBLIC_ALLOWED_ROOT_ENTRIES:
        return True
    if any(is_excluded_directory_name(part) for part in lowered):
        return True
    if lowered[0] in EXCLUDED_ROOT_DIRECTORIES:
        return True
    if (
        lowered[0] == "work"
        and len(lowered) > 1
        and lowered[1] not in ALLOWED_WORK_ENTRIES
    ):
        return True
    if any(
        lowered[: len(prefix)] == prefix
        for prefix in EXCLUDED_NESTED_DIRECTORY_PATHS
    ):
        return True
    if PNPM_STORE_DIRECTORY_NAME in lowered:
        return True
    if lowered[: len(OPENCLAW_NODE_MODULES_PREFIX)] == OPENCLAW_NODE_MODULES_PREFIX:
        nested_parts = lowered[len(OPENCLAW_NODE_MODULES_PREFIX) :]
        if ".bin" in nested_parts:
            return True
    return False


def iter_release_paths(
    source: Path,
    *,
    include_private_keys: bool = False,
    include_private_history: bool = False,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(physical path, archive-relative path)`` release entries.

    pnpm installs dependencies as directory links into its package-local
    ``node_modules/.pnpm`` store. Those links are required for the bundled
    OpenClaw runtime, but ZIP readers on Windows do not recreate them safely.
    The iterator therefore expands only verified package-local pnpm links into
    ordinary archive entries. Every other link remains forbidden.
    """

    def walk(
        directory: Path,
        relative_directory: Path,
        active_directories: frozenset[Path],
    ) -> Iterator[tuple[Path, Path]]:
        canonical_directory = directory.resolve(strict=True)
        if canonical_directory != directory:
            raise ValueError(
                "Release traversal encountered an unverified filesystem redirect: "
                f"{relative_directory}"
            )
        if canonical_directory in active_directories:
            raise ValueError(
                "Package-local pnpm link cycle cannot be materialized: "
                f"{relative_directory}"
            )
        branch_directories = active_directories | {canonical_directory}
        for path in sorted(
            directory.iterdir(),
            key=lambda item: (item.name.casefold(), item.name),
        ):
            relative = relative_directory / path.name
            filesystem_link = is_filesystem_link(path)
            entry_excluded = excluded(
                relative,
                include_private_keys=include_private_keys,
                include_private_history=include_private_history,
            )
            if filesystem_link:
                if entry_excluded:
                    continue
                target = materialized_pnpm_link_target(source, path, relative)
                yield target, relative
                yield from walk(
                    target,
                    relative,
                    branch_directories,
                )
                continue
            if not entry_excluded:
                yield path, relative
            if not path.is_dir():
                continue
            if entry_excluded and excluded_directory_tree(
                relative,
                include_private_keys=include_private_keys,
                include_private_history=include_private_history,
            ):
                continue
            yield from walk(
                path,
                relative,
                branch_directories,
            )

    yield from walk(source, Path(), frozenset())


def materialized_pnpm_link_target(
    source: Path,
    link: Path,
    relative: Path,
) -> Path:
    """Resolve one safe pnpm directory link without exposing its target path."""

    lowered = tuple(part.casefold() for part in relative.parts)
    if (
        len(lowered) <= len(OPENCLAW_NODE_MODULES_PREFIX)
        or lowered[: len(OPENCLAW_NODE_MODULES_PREFIX)]
        != OPENCLAW_NODE_MODULES_PREFIX
    ):
        raise ValueError(
            "Release archives do not permit symbolic links or junctions: "
            f"{relative}"
        )
    try:
        modules_root = source.joinpath(*OPENCLAW_NODE_MODULES_PREFIX).resolve(
            strict=True
        )
        target = link.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(
            f"Package-local pnpm link is broken or cyclic: {relative}"
        ) from None
    is_junction = getattr(link, "is_junction", None)
    if not link.is_symlink() and not (
        is_junction is not None and is_junction()
    ):
        raise ValueError(
            f"Unsupported filesystem reparse point in release input: {relative}"
        )
    if target == modules_root or not target.is_relative_to(modules_root):
        raise ValueError(
            "Package-local pnpm links must remain inside OpenClaw node_modules: "
            f"{relative}"
        )
    target_relative = target.relative_to(source)
    target_lowered = tuple(part.casefold() for part in target_relative.parts)
    if (
        len(target_lowered) <= len(OPENCLAW_PNPM_STORE_PREFIX)
        or not is_openclaw_pnpm_store_path(target_lowered)
        or excluded(target_relative, allow_pnpm_store_source=True)
    ):
        raise ValueError(
            "Package-local pnpm links must resolve into the trusted .pnpm store: "
            f"{relative}"
        )
    if not target.is_dir():
        raise ValueError(
            f"Package-local pnpm links must target directories: {relative}"
        )
    return target


def validate_release_entry_source(source: Path, path: Path, relative: Path) -> None:
    """Fail if an entry changed into a redirect after release enumeration."""

    if is_filesystem_link(path):
        raise ValueError(
            "Release archives do not permit unresolved filesystem links: "
            f"{relative}"
        )
    try:
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"Release input disappeared while building: {relative}") from None
    if canonical != path or not canonical.is_relative_to(source):
        raise ValueError(
            "Release input changed into an unverified filesystem redirect: "
            f"{relative}"
        )


def is_public_machine_local_venv_file(lowered: tuple[str, ...]) -> bool:
    if len(lowered) >= 3 and lowered[:2] == (".venv", "scripts"):
        name = lowered[-1]
        if name in PUBLIC_VENV_ACTIVATION_FILES:
            return True
        # distlib's Windows console launchers append an absolute ``#!...``
        # interpreter path to every generated .exe.  Copying those wrappers
        # leaks the build account/install directory and the wrappers stop
        # working once the package moves.  Elren invokes dependencies through
        # ``python -m`` and keeps only the real relocatable venv interpreters;
        # package-specific console entry points must therefore not cross the
        # release boundary.
        return name.endswith(".exe") and name not in PORTABLE_VENV_PYTHON_EXECUTABLES
    if len(lowered) < 4 or lowered[:3] != (".venv", "lib", "site-packages"):
        return False
    if lowered[-1] in {
        "_editable_impl_elren_agent.pth",
        "_editable_impl_milo_agent.pth",
        "_editable_impl_qelvane_desk_agent.pth",
    }:
        return True
    return (
        lowered[-1] == "direct_url.json"
        and len(lowered) >= 5
        and lowered[-2].startswith(("elren_agent-", "milo_agent-", "qelvane_desk_agent-"))
        and lowered[-2].endswith(".dist-info")
    )


def portable_venv_config(contents: str) -> str:
    match = re.search(r"(?m)^version\s*=\s*(\d+\.\d+(?:\.\d+)?)\s*$", contents)
    if not match:
        raise ValueError("The packaged .venv/pyvenv.cfg has no Python version")
    return (
        "include-system-site-packages = false\n"
        f"version = {match.group(1)}\n"
    )


def portable_python_script(contents: str) -> str:
    """Replace a build-machine Python shebang with package PATH discovery."""

    lines = contents.splitlines(keepends=True)
    if not lines:
        return contents
    first = lines[0]
    if not first.startswith("#!") or not re.search(
        r"(?i)(?:^|[\\/])python(?:\.exe)?\s*$", first.strip()
    ):
        return contents
    ending = "\r\n" if first.endswith("\r\n") else "\n"
    lines[0] = f"#!python{ending}"
    return "".join(lines)


def portable_python_dependency_stamp(source: Path) -> bytes | None:
    """Generate the exact location-independent warm-start stamp for a bundle."""

    config_path = source / ".venv" / "pyvenv.cfg"
    manifest_path = source / "pyproject.toml"
    if not config_path.is_file() or not manifest_path.is_file():
        return None
    config = config_path.read_text(encoding="utf-8")
    match = re.search(r"(?m)^version\s*=\s*(\d+\.\d+(?:\.\d+)?)\s*$", config)
    if not match:
        raise ValueError("The packaged .venv/pyvenv.cfg has no Python version")
    fingerprint = hashlib.sha256(manifest_path.read_bytes()).hexdigest().upper()
    return f"{match.group(1)}|{fingerprint}".encode()


def portable_node_dependency_stamp(source: Path) -> bytes | None:
    """Match the Windows cold-path bundle check before refreshing its cache.

    Payload overlays can replace package.json after the source environment was
    bootstrapped. Shipping that environment's old hash forces a needless full
    discovery pass after every release. Never mark an incomplete/mismatched
    runtime ready, and hash the exact bytes that will ship.
    """
    def verified_input(path: Path) -> Path:
        # Source workspaces may use pnpm junctions; archives materialize them.
        # Follow only the same checked package-local .pnpm links as the builder,
        # never an arbitrary resolve() into an external runtime.
        relative = path.relative_to(source)
        cursor = source
        for index, part in enumerate(relative.parts):
            cursor /= part
            if is_filesystem_link(cursor):
                cursor = materialized_pnpm_link_target(source, cursor, Path(*relative.parts[:index + 1]))
        validate_release_entry_source(source, cursor, relative)
        return cursor

    runtime = source / "work/openclaw-runtime"
    required = (
        source / "work/runtime-bundle.json",
        source / "work/node-runtime/node.exe",
        runtime / "package.json",
        runtime / "node_modules/openclaw/package.json",
        runtime / "node_modules/openclaw/openclaw.mjs",
        runtime / "node_modules/@modelcontextprotocol/sdk/package.json",
        runtime / "node_modules/.modules.yaml",
    )
    if not all(path.is_file() for path in required):
        return None
    required = tuple(verified_input(path) for path in required)
    skills = runtime / "node_modules/openclaw/skills"
    if not skills.is_dir():
        return None
    verified_input(skills)
    try:
        bundle = json.loads(required[0].read_text(encoding="utf-8"))
        manifest = json.loads(required[2].read_text(encoding="utf-8"))
        installed = json.loads(required[3].read_text(encoding="utf-8"))["version"]
        desired = manifest["dependencies"]["openclaw"]
        node_version = bundle["components"]["node"]["version"]
        if (
            bundle["schema"] != 1
            or bundle["bundle"] != "elren-portable-runtime"
            or not re.fullmatch(r"\d+\.\d+\.\d+", node_version)
            or installed != desired
            or installed != bundle["components"]["openclaw"]["version"]
            or not re.search(r'(?m)nodeLinker"?\s*:\s*"?hoisted', required[-1].read_text(encoding="utf-8"))
        ):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    parts = [node_version, desired]
    for name in ("package.json", "pnpm-lock.yaml"):
        path = runtime / name
        if path.is_file():
            parts.append(hashlib.sha256(verified_input(path).read_bytes()).hexdigest().upper())
    return "|".join(parts).encode("utf-8")


def write_reproducible_sqlite_snapshot(
    archive: zipfile.ZipFile,
    path: Path,
    archive_name: str,
) -> int:
    """Resolve committed WAL frames into one consistent DB; omit live sidecars."""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".snapshot", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    source_connection: sqlite3.Connection | None = None
    snapshot_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            f"{path.resolve(strict=True).as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
        snapshot_connection = sqlite3.connect(temporary)
        source_connection.backup(snapshot_connection)
        result = snapshot_connection.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).casefold() != "ok":
            raise ValueError("data/deepdesk.db failed SQLite integrity verification")
        snapshot_connection.close()
        snapshot_connection = None
        source_connection.close()
        source_connection = None
        write_reproducible_file(archive, temporary, archive_name)
        return temporary.stat().st_size
    except sqlite3.Error as exc:
        raise ValueError("data/deepdesk.db is not a valid SQLite database") from exc
    finally:
        if snapshot_connection is not None:
            snapshot_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)


def portable_pnpm_modules_state(contents: str) -> str:
    """Remove pnpm's build-host store paths while retaining runtime metadata."""

    state = json.loads(contents)
    if not isinstance(state, dict):
        raise ValueError(f"{PNPM_MODULES_STATE_NAME} must contain an object")
    state.pop("storeDir", None)
    state.pop("virtualStoreDir", None)
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def validate_public_env_template(contents: str) -> None:
    """Reject a public environment template that contains an actual credential."""

    validate_env_has_no_plaintext_credentials(contents, PUBLIC_ENV_TEMPLATE_NAME)


def validate_env_has_no_plaintext_credentials(contents: str, source_name: str) -> None:
    """Reject non-empty credential assignments in a release environment file."""

    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(
                f"{source_name}:{line_number} is not a KEY=VALUE entry"
            )
        key, raw_value = line.split("=", 1)
        normalized_key = key.strip().casefold()
        value = raw_value.strip().strip("'\"")
        if PUBLIC_ENV_CREDENTIAL_KEY.search(normalized_key) and value:
            raise ValueError(
                f"{source_name}:{line_number} contains a non-empty "
                f"credential field ({key.strip()})"
            )


def validate_runtime_settings_no_plaintext_credentials(contents: str) -> None:
    """Reject credentials left in the legacy plaintext runtime-settings JSON."""

    try:
        payload = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise ValueError("data/runtime-settings.json is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("data/runtime-settings.json must contain an object")

    def has_value(value: object) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict, tuple, set)):
            return bool(value)
        return bool(value)

    def inspect(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key)
                normalized = key.casefold()
                if has_value(child) and (
                    normalized in LEGACY_RUNTIME_PRIVATE_KEYS
                    or PUBLIC_ENV_CREDENTIAL_KEY.search(normalized)
                ):
                    location = ".".join((*path, key))
                    raise ValueError(
                        "data/runtime-settings.json contains a non-empty "
                        f"plaintext credential field ({location})"
                    )
                inspect(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, (*path, str(index)))

    inspect(payload, ())


def should_validate_first_party_text(relative: Path) -> bool:
    """Return whether a portable first-party payload file should be path-scanned."""

    if relative.suffix.casefold() not in FIRST_PARTY_TEXT_SUFFIXES:
        return False
    if len(relative.parts) == 1:
        return True
    return relative.parts[0].casefold() in FIRST_PARTY_TEXT_ROOTS


def validate_release_text_portability(relative: Path, contents: str) -> None:
    """Reject build-host paths accidentally copied into first-party release text."""

    if MACHINE_LOCAL_CLIPBOARD_REFERENCE.search(contents):
        raise ValueError(f"{relative} contains a machine-local clipboard path")

    normalized = contents.replace("\\", "/").casefold()
    candidate_homes = {Path.home()}
    user_profile = os.environ.get("USERPROFILE", "").strip()
    if user_profile:
        candidate_homes.add(Path(user_profile))
    for home in candidate_homes:
        marker = str(home).replace("\\", "/").rstrip("/").casefold()
        if marker and marker != "/" and marker in normalized:
            raise ValueError(f"{relative} contains the build user's absolute home path")


def reproducible_zip_info(
    archive_name: str,
    *,
    is_dir: bool = False,
    executable: bool = False,
) -> zipfile.ZipInfo:
    """Return stable ZIP metadata independent of the build host and source mtimes."""

    normalized = archive_name.rstrip("/") + "/" if is_dir else archive_name
    info = zipfile.ZipInfo(normalized, date_time=REPRODUCIBLE_ZIP_TIME)
    info.create_system = 3
    mode = 0o40755 if is_dir else (0o100755 if executable else 0o100644)
    info.external_attr = (mode & 0xFFFF) << 16
    if is_dir:
        info.external_attr |= 0x10
    info.compress_type = zipfile.ZIP_STORED if is_dir else zipfile.ZIP_DEFLATED
    return info


def write_reproducible_bytes(
    archive: zipfile.ZipFile,
    archive_name: str,
    data: bytes,
    *,
    executable: bool = False,
) -> None:
    archive.writestr(
        reproducible_zip_info(archive_name, executable=executable),
        data,
    )


def write_reproducible_file(
    archive: zipfile.ZipFile,
    path: Path,
    archive_name: str,
    *,
    executable: bool = False,
) -> None:
    info = reproducible_zip_info(archive_name, executable=executable)
    with path.open("rb") as source_handle, archive.open(
        info, mode="w", force_zip64=True
    ) as archive_handle:
        shutil.copyfileobj(source_handle, archive_handle, length=1024 * 1024)


def build(
    source: Path,
    destination: Path,
    *,
    include_private_keys: bool = False,
    include_private_history: bool = False,
) -> tuple[int, int]:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if destination == source or source in destination.parents:
        raise ValueError("The release archive destination must be outside the source tree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".building", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    files = 0
    total_bytes = 0
    python_dependency_stamp = portable_python_dependency_stamp(source)
    wrote_python_dependency_stamp = False
    node_dependency_stamp = portable_node_dependency_stamp(source)
    wrote_node_dependency_stamp = False
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            root_name = source.name
            archive.writestr(
                reproducible_zip_info(f"{root_name}/", is_dir=True), b""
            )
            for path, relative in sorted(
                iter_release_paths(
                    source,
                    include_private_keys=include_private_keys,
                    include_private_history=include_private_history,
                ),
                key=lambda item: (
                    item[1].as_posix().casefold(),
                    item[1].as_posix(),
                ),
            ):
                if excluded(
                    relative,
                    include_private_keys=include_private_keys,
                    include_private_history=include_private_history,
                ):
                    continue
                validate_release_entry_source(source, path, relative)
                archive_name = (Path(root_name) / relative).as_posix()
                if (
                    tuple(part.casefold() for part in relative.parts) == PRIVATE_DESKTOP_LANGUAGE_FILE
                    and not path.is_file()
                ):
                    raise ValueError("data/desktop-ui-language.txt must be a regular file")
                if path.is_dir():
                    archive.writestr(
                        reproducible_zip_info(archive_name, is_dir=True), b""
                    )
                    continue
                first_party_contents: str | None = None
                if should_validate_first_party_text(relative):
                    first_party_contents = path.read_text(encoding="utf-8")
                    validate_release_text_portability(relative, first_party_contents)
                written_size = path.stat().st_size
                if (
                    tuple(part.casefold() for part in relative.parts)
                    == (".venv", "pyvenv.cfg")
                ):
                    write_reproducible_bytes(
                        archive,
                        archive_name,
                        portable_venv_config(path.read_text(encoding="utf-8")).encode("utf-8"),
                    )
                elif (
                    tuple(part.casefold() for part in relative.parts)
                    == PYTHON_DEPENDENCY_STAMP
                    and python_dependency_stamp is not None
                ):
                    write_reproducible_bytes(
                        archive,
                        archive_name,
                        python_dependency_stamp,
                    )
                    wrote_python_dependency_stamp = True
                elif (
                    tuple(part.casefold() for part in relative.parts) == NODE_DEPENDENCY_STAMP
                    and node_dependency_stamp is not None
                ):
                    write_reproducible_bytes(archive, archive_name, node_dependency_stamp)
                    written_size = len(node_dependency_stamp)
                    wrote_node_dependency_stamp = True
                elif (
                    len(relative.parts) >= 3
                    and tuple(part.casefold() for part in relative.parts[:2])
                    == (".venv", "scripts")
                    and relative.suffix.casefold() in {"", ".py"}
                ):
                    write_reproducible_bytes(
                        archive,
                        archive_name,
                        portable_python_script(path.read_text(encoding="utf-8")).encode(
                            "utf-8"
                        ),
                    )
                elif (
                    tuple(part.casefold() for part in relative.parts)
                    == OPENCLAW_NODE_MODULES_PREFIX + (PNPM_MODULES_STATE_NAME,)
                ):
                    write_reproducible_bytes(
                        archive,
                        archive_name,
                        portable_pnpm_modules_state(
                            path.read_text(encoding="utf-8")
                        ).encode("utf-8"),
                    )
                elif (
                    not include_private_keys
                    and relative.name.casefold() == PUBLIC_ENV_TEMPLATE_NAME
                ):
                    contents = (
                        first_party_contents
                        if first_party_contents is not None
                        else path.read_text(encoding="utf-8")
                    )
                    validate_public_env_template(contents)
                    write_reproducible_bytes(
                        archive, archive_name, contents.encode("utf-8")
                    )
                elif include_private_keys and tuple(
                    part.casefold() for part in relative.parts
                ) == (".env",):
                    contents = (
                        first_party_contents
                        if first_party_contents is not None
                        else path.read_text(encoding="utf-8")
                    )
                    validate_env_has_no_plaintext_credentials(contents, ".env")
                    write_reproducible_bytes(
                        archive, archive_name, contents.encode("utf-8")
                    )
                elif include_private_keys and tuple(
                    part.casefold() for part in relative.parts
                ) == ("data", "provider-secrets.vault"):
                    with path.open("rb") as handle:
                        if handle.read(len(PROVIDER_VAULT_MAGIC)) != PROVIDER_VAULT_MAGIC:
                            raise ValueError(
                                "data/provider-secrets.vault is not an encrypted Elren vault"
                            )
                    write_reproducible_file(archive, path, archive_name)
                elif include_private_keys and tuple(
                    part.casefold() for part in relative.parts
                ) == ("data", "runtime-settings.json"):
                    contents = path.read_text(encoding="utf-8")
                    validate_runtime_settings_no_plaintext_credentials(contents)
                    write_reproducible_bytes(
                        archive, archive_name, contents.encode("utf-8")
                    )
                elif include_private_keys and include_private_history and tuple(
                    part.casefold() for part in relative.parts
                ) == PRIVATE_DESKTOP_LANGUAGE_FILE:
                    language = read_private_desktop_language(path)
                    written_size = len(language)
                    write_reproducible_bytes(archive, archive_name, language)
                elif include_private_history and tuple(
                    part.casefold() for part in relative.parts
                ) == ("data", "deepdesk.db"):
                    written_size = write_reproducible_sqlite_snapshot(
                        archive, path, archive_name
                    )
                else:
                    write_reproducible_file(archive, path, archive_name)
                files += 1
                total_bytes += written_size
            if python_dependency_stamp is not None and not wrote_python_dependency_stamp:
                archive_name = (
                    Path(root_name) / Path(*PYTHON_DEPENDENCY_STAMP)
                ).as_posix()
                write_reproducible_bytes(
                    archive,
                    archive_name,
                    python_dependency_stamp,
                )
                files += 1
                total_bytes += len(python_dependency_stamp)
            if node_dependency_stamp is not None and not wrote_node_dependency_stamp:
                archive_name = (Path(root_name) / Path(*NODE_DEPENDENCY_STAMP)).as_posix()
                write_reproducible_bytes(archive, archive_name, node_dependency_stamp)
                files += 1
                total_bytes += len(node_dependency_stamp)
            if include_private_history:
                notice_name = (Path(root_name) / PRIVATE_PROFILE_NOTICE).as_posix()
                write_reproducible_bytes(
                    archive,
                    notice_name,
                    PRIVATE_PROFILE_NOTICE_CONTENT.encode("utf-8"),
                )
                files += 1
                total_bytes += len(PRIVATE_PROFILE_NOTICE_CONTENT.encode("utf-8"))
        with zipfile.ZipFile(temporary) as archive:
            broken = archive.testzip()
            if broken:
                raise RuntimeError(f"CRC verification failed for {broken}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return files, total_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a portable Elren ZIP archive.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--include-private-keys",
        action="store_true",
        help=(
            "Include only the owner's encrypted provider vault and scrubbed "
            "runtime settings; this legacy-compatible flag never includes history."
        ),
    )
    parser.add_argument(
        "--include-private-history",
        action="store_true",
        help=(
            "Include a consistent but UNENCRYPTED personal history database "
            "snapshot. The resulting archive is highly sensitive and owner-only."
        ),
    )
    parser.add_argument(
        "--include-private-profile",
        action="store_true",
        help=(
            "Build the owner-only profile with encrypted credentials plus an "
            "UNENCRYPTED personal history database; never redistribute it."
        ),
    )
    args = parser.parse_args()
    include_private_keys = args.include_private_keys or args.include_private_profile
    include_private_history = (
        args.include_private_history or args.include_private_profile
    )
    if include_private_history:
        print(
            "WARNING: this owner-only archive contains an unencrypted personal "
            "history database. Do not share it.",
            file=sys.stderr,
        )
    files, total_bytes = build(
        args.source,
        args.destination,
        include_private_keys=include_private_keys,
        include_private_history=include_private_history,
    )
    print(f"archive={args.destination.resolve()} files={files} source_bytes={total_bytes}")


if __name__ == "__main__":
    main()
