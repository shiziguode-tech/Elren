from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from launcher.build_release_archive import build, is_filesystem_link
from launcher.sanitize_public_package import sanitize


def test_release_archive_preserves_unicode_and_omits_volatile_profile(tmp_path: Path):
    package = tmp_path / "package-v1.0-public"
    (package / "data" / "webview2" / "Default").mkdir(parents=True)
    (package / "outputs").mkdir()
    (package / "deepdesk" / "__pycache__").mkdir(parents=True)
    (package / "skills" / "demo" / ".ruff_cache").mkdir(parents=True)
    (package / "mobile" / "ElrenMobile" / "app" / "build").mkdir(parents=True)
    (package / "mobile" / "ElrenMobile" / ".gradle-corrupt-20260813").mkdir(parents=True)
    (package / "work" / "studio").mkdir(parents=True)
    (package / "work" / "sandbox").mkdir(parents=True)
    (package / "work" / "qa-workspace").mkdir(parents=True)
    (package / "work" / "openclaw-runtime" / "node_modules" / ".ignored_openclaw").mkdir(parents=True)
    (package / "work" / "openclaw-runtime" / "node_modules" / ".bin").mkdir(
        parents=True
    )
    (package / "work/openclaw-runtime/node_modules/.pnpm/store-index").mkdir(
        parents=True
    )
    (package / "work/openclaw-runtime/node_modules/.pnpm/store-index/cache.json").write_text(
        '{"cache":true}', encoding="utf-8"
    )
    (package / "work" / "openclaw-runtime" / "skills-check.json").write_text(
        '{"workspaceDir":"C:\\\\Users\\\\Builder\\\\.openclaw\\\\workspace"}',
        encoding="utf-8",
    )
    (package / "work" / "openclaw-runtime" / "node_modules" / ".bin" / "openclaw").write_text(
        "#!/bin/sh\n# cmd-shim-target=C:\\Users\\Builder\\openclaw.mjs\n",
        encoding="utf-8",
    )
    (package / "work" / "openclaw-runtime" / "node_modules" / ".modules.yaml").write_text(
        '{"nodeLinker":"hoisted","storeDir":"C:\\\\Users\\\\Builder\\\\pnpm-store",'
        '"virtualStoreDir":"C:\\\\Users\\\\Builder\\\\package\\\\node_modules\\\\.pnpm"}',
        encoding="utf-8",
    )
    (
        package
        / "work"
        / "openclaw-runtime"
        / "node_modules"
        / ".pnpm-workspace-state-v1.json"
    ).write_text(
        '{"projects":{"C:\\\\Users\\\\Builder\\\\package":{"name":"sidecar"}}}',
        encoding="utf-8",
    )
    (package / "work" / "tool-runtime" / "bin").mkdir(parents=True)
    (package / "work" / "browser-runtime" / "chromium").mkdir(parents=True)
    (package / ".venv" / "Scripts").mkdir(parents=True)
    (package / "dist" / "package-v8-private" / "data").mkdir(parents=True)
    (package / "README.md").write_text("ready", encoding="utf-8")
    (package / ".venv" / "Scripts" / "pygrun").write_text(
        "#!C:\\Users\\Builder\\Python\\python.exe\nprint('ok')\n",
        encoding="utf-8",
    )
    (package / "RELEASE-NOTES.md").write_text("v1.0 candidate", encoding="utf-8")
    (package / "THIRD_PARTY_NOTICES.md").write_text(
        "third-party inventory", encoding="utf-8"
    )
    (package / ".env.example").write_text(
        "DEEPSEEK_API_KEY=\nDEEPSEEK_BASE_URL=https://api.deepseek.com\n"
        "ELREN_MAX_OUTPUT_TOKENS=0\n",
        encoding="utf-8",
    )
    (package / "python-installer.exe").write_bytes(b"installer")
    (package / "USER.md").write_text("private profile", encoding="utf-8")
    (package / "memory").mkdir()
    (package / "memory/private.md").write_text("private memory", encoding="utf-8")
    (package / "unrelated-notes.txt").write_text("private notes", encoding="utf-8")
    (package / ".env").write_text("SECRET=private", encoding="utf-8")
    (package / "data" / "provider-keys.json").write_text("secret", encoding="utf-8")
    (package / "data" / "provider-secrets.vault").write_bytes(b"encrypted-secret")
    (package / "outputs" / "private.txt").write_text("private", encoding="utf-8")
    (package / "skills" / "demo" / ".ruff_cache" / "cache").write_bytes(b"cache")
    (package / "mobile" / "ElrenMobile" / "local.properties").write_text(
        "sdk.dir=C:\\\\Users\\\\builder", encoding="utf-8"
    )
    (package / "mobile" / "ElrenMobile" / "app" / "build" / "generated.bin").write_bytes(
        b"generated"
    )
    (package / "mobile" / "ElrenMobile" / ".gradle-corrupt-20260813" / "cache.bin").write_bytes(
        b"stale cache"
    )
    (package / "work" / "studio" / "stale.txt").write_text("removed", encoding="utf-8")
    (package / "work" / "sandbox" / "session.txt").write_text("removed", encoding="utf-8")
    (package / "work" / "qa-workspace" / "deepdesk.db").write_bytes(b"history")
    (package / "work" / "openclaw-runtime" / "node_modules" / ".ignored_openclaw" / "old.js").write_text(
        "obsolete", encoding="utf-8"
    )
    (package / "work" / "tool-runtime" / "manifest.json").write_text(
        '{"schema":1,"bundle":"elren-tool-runtime","product_version":"1.0"}',
        encoding="utf-8",
    )
    (package / "work" / "tool-runtime" / "bin" / "tool.cmd").write_text(
        "@echo off", encoding="utf-8"
    )
    (package / "work" / "browser-runtime" / "chromium" / "debug.log").write_text(
        "machine-local browser log", encoding="utf-8"
    )
    (package / "work" / "openclaw-runtime" / "upstream-debug.pdb").write_bytes(
        b"C:\\Users\\BuildAgent\\AppData\\Local\\Temp\\compiler-state"
    )
    (package / "dist" / "package-v8-private" / "data" / "provider-keys.json").write_text(
        "secret", encoding="utf-8"
    )
    (package / "data" / "webview2" / "Default" / "Cookies").write_bytes(b"volatile")
    (package / "deepdesk" / "__pycache__" / "module.pyc").write_bytes(b"cache")

    destination = tmp_path / "release.zip"
    files, total_bytes = build(package, destination)

    assert files == 8
    assert total_bytes > len(b"ready")
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert "package-v1.0-public/README.md" in names
        assert "package-v1.0-public/RELEASE-NOTES.md" in names
        assert "package-v1.0-public/THIRD_PARTY_NOTICES.md" in names
        assert archive.read(f"{package.name}/.venv/Scripts/pygrun").decode(
            "utf-8"
        ) == "#!python\nprint('ok')\n"
        assert "package-v1.0-public/.env.example" in names
        assert "package-v1.0-public/python-installer.exe" not in names
        assert "package-v1.0-public/USER.md" not in names
        assert not any("/memory/" in name.lower() for name in names)
        assert "package-v1.0-public/unrelated-notes.txt" not in names
        assert not any("webview2" in name.lower() for name in names)
        assert not any("__pycache__" in name.lower() for name in names)
        assert not any(".ruff_cache" in name.lower() for name in names)
        assert not any("local.properties" in name.lower() for name in names)
        assert not any(name.endswith("/.env") for name in names)
        assert not any("/data/" in name.lower() for name in names)
        assert not any("/outputs/" in name.lower() for name in names)
        assert not any("/app/build/" in name.lower() for name in names)
        assert not any(".gradle-corrupt-" in name.lower() for name in names)
        assert not any("/work/studio/" in name.lower() for name in names)
        assert not any("/work/sandbox/" in name.lower() for name in names)
        assert not any("/work/qa-workspace/" in name.lower() for name in names)
        assert not any(".ignored_openclaw" in name.lower() for name in names)
        assert not any("/node_modules/.bin/" in name.casefold() for name in names)
        assert not any("/node_modules/.pnpm/" in name.casefold() for name in names)
        assert not any(
            name.casefold().endswith("/.pnpm-workspace-state-v1.json")
            for name in names
        )
        assert not any(name.casefold().endswith("/skills-check.json") for name in names)
        assert not any("/dist/" in name.lower() for name in names)
        assert not any("provider-keys.json" in name.lower() for name in names)
        assert not any("provider-secrets.vault" in name.lower() for name in names)
        assert f"{package.name}/work/tool-runtime/manifest.json" in names
        assert f"{package.name}/work/tool-runtime/bin/tool.cmd" in names
        modules_state = archive.read(
            f"{package.name}/work/openclaw-runtime/node_modules/.modules.yaml"
        ).decode("utf-8")
        assert '"nodeLinker": "hoisted"' in modules_state
        assert "storeDir" not in modules_state
        assert "virtualStoreDir" not in modules_state
        assert "C:\\Users\\Builder" not in modules_state
        assert not any(name.casefold().endswith(".log") for name in names)
        assert not any(name.casefold().endswith(".pdb") for name in names)


def test_release_archive_does_not_descend_into_excluded_directory_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package-v1.0-public"
    blocked_directories = {
        package / "qa-artifacts",
        package / "outputs",
        package / "deepdesk" / "__pycache__",
        package / "work" / "studio",
        package / "work" / "openclaw-runtime" / "node_modules" / ".bin",
    }
    for directory in blocked_directories:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "must-not-be-visited.txt").write_text(
            "excluded",
            encoding="utf-8",
        )
    (package / "README.md").write_text("portable", encoding="utf-8")
    (package / "deepdesk" / "keep.py").write_text("KEEP = True\n", encoding="utf-8")

    original_scandir = os.scandir

    def guarded_scandir(path):
        candidate = Path(path)
        if candidate in blocked_directories:
            raise AssertionError(f"release traversal entered excluded directory: {path}")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)

    destination = tmp_path / "release.zip"
    build(package, destination)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert f"{package.name}/README.md" in names
        assert f"{package.name}/deepdesk/keep.py" in names
        assert not any("must-not-be-visited.txt" in name for name in names)


def test_legacy_hyphenated_archive_entrypoint_delegates_to_canonical_builder(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package-v1.0-public"
    package.mkdir()
    (package / "README.md").write_text("portable", encoding="utf-8")
    destination = tmp_path / "release.zip"

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "launcher" / "build-release-archive.py"),
            str(package),
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert destination.is_file()
    assert "files=1" in result.stdout


def test_private_profile_includes_keys_and_history_but_not_logs(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-private"
    (package / "data").mkdir(parents=True)
    (package / ".env").write_text(
        "DEEPSEEK_API_KEY=\nELREN_PORT=8765\n", encoding="utf-8"
    )
    (package / "data/provider-secrets.vault").write_bytes(b"ELRENVLT\x01encrypted")
    (package / "data/provider-keys.json").write_text(
        '{"gemini":"legacy-plaintext-must-not-ship"}', encoding="utf-8"
    )
    (package / "data/runtime-settings.json").write_text('{"model":"auto"}', encoding="utf-8")
    with sqlite3.connect(package / "data/deepdesk.db") as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, payload TEXT)")
        connection.execute(
            "INSERT INTO tasks(id, payload) VALUES (?, ?)",
            ("private-chat", '{"title":"private chat history"}'),
        )
    (package / "data/launcher.log").write_text("private log", encoding="utf-8")
    browser_runtime = package / "work/browser-runtime/chromium"
    browser_runtime.mkdir(parents=True)
    (browser_runtime / "debug.log").write_text("machine-local browser log", encoding="utf-8")
    pnpm_store = package / "work/openclaw-runtime/node_modules/.pnpm/cache-index"
    pnpm_store.mkdir(parents=True)
    (pnpm_store / "state.json").write_text('{"cache":true}', encoding="utf-8")
    (package / "README.md").write_text("keep", encoding="utf-8")
    (package / "USER.md").write_text("private profile", encoding="utf-8")
    (package / "memory").mkdir()
    (package / "memory/private.md").write_text("private memory", encoding="utf-8")

    destination = tmp_path / "private.zip"
    build(
        package,
        destination,
        include_private_keys=True,
        include_private_history=True,
    )

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        root = package.name
        assert {name.split("/", 1)[0] for name in names if name} == {root}
        assert f"{root}/.env" in names
        assert f"{root}/data/provider-secrets.vault" in names
        assert f"{root}/data/provider-keys.json" not in names
        assert f"{root}/data/runtime-settings.json" in names
        assert f"{root}/PRIVATE-DATA-NOTICE.txt" in names
        database_name = f"{root}/data/deepdesk.db"
        assert database_name in names
        assert f"{root}/data/launcher.log" not in names
        assert f"{root}/work/browser-runtime/chromium/debug.log" not in names
        assert not any("/node_modules/.pnpm/" in name.casefold() for name in names)
        assert f"{root}/USER.md" not in names
        assert not any("/memory/" in name.casefold() for name in names)
        extracted_database = tmp_path / "archived-deepdesk.db"
        extracted_database.write_bytes(archive.read(database_name))
    with sqlite3.connect(extracted_database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT id FROM tasks").fetchall() == [
            ("private-chat",)
        ]


def test_private_keys_opt_in_never_silently_includes_history(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-private"
    (package / "data").mkdir(parents=True)
    (package / ".env").write_text("ELREN_PORT=8765\n", encoding="utf-8")
    (package / "data/provider-secrets.vault").write_bytes(b"ELRENVLT\x01encrypted")
    (package / "data/runtime-settings.json").write_text("{}", encoding="utf-8")
    with sqlite3.connect(package / "data/deepdesk.db") as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    (package / "README.md").write_text("keep", encoding="utf-8")

    destination = tmp_path / "private-keys.zip"
    build(package, destination, include_private_keys=True)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        root = package.name
        assert f"{root}/data/provider-secrets.vault" in names
        assert f"{root}/data/deepdesk.db" not in names
        assert f"{root}/PRIVATE-DATA-NOTICE.txt" not in names


def test_private_history_snapshot_resolves_committed_wal_only(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-private"
    data = package / "data"
    data.mkdir(parents=True)
    database = data / "deepdesk.db"
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO tasks(id) VALUES ('committed-in-wal')")
        writer.commit()
        wal = data / "deepdesk.db-wal"
        assert wal.is_file() and wal.stat().st_size > 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO tasks(id) VALUES ('uncommitted')")

        (package / "README.md").write_text("keep", encoding="utf-8")
        destination = tmp_path / "private-history.zip"
        build(package, destination, include_private_history=True)
    finally:
        writer.rollback()
        writer.close()

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        root = package.name
        database_name = f"{root}/data/deepdesk.db"
        assert database_name in names
        assert f"{root}/data/deepdesk.db-wal" not in names
        assert f"{root}/data/deepdesk.db-shm" not in names
        snapshot = tmp_path / "wal-snapshot.db"
        snapshot.write_bytes(archive.read(database_name))
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT id FROM tasks").fetchall() == [
            ("committed-in-wal",)
        ]


@pytest.mark.parametrize("source_stamp", [None, "3.14.6|STALE"])
def test_release_archive_generates_current_python_warm_start_stamp(
    tmp_path: Path,
    source_stamp: str | None,
) -> None:
    package = tmp_path / "package-v1.0-public"
    (package / ".venv").mkdir(parents=True)
    (package / ".venv/pyvenv.cfg").write_text(
        "home = C:\\Users\\Builder\\Python\n"
        "include-system-site-packages = false\n"
        "version = 3.14.6\n",
        encoding="utf-8",
    )
    if source_stamp is not None:
        (package / ".venv/.elren-python-dependencies-v2").write_text(
            source_stamp,
            encoding="utf-8",
        )
    manifest = b"[project]\nname='elren-agent'\nversion='1.0.0'\n"
    (package / "pyproject.toml").write_bytes(manifest)

    destination = tmp_path / "release.zip"
    build(package, destination)

    expected = f"3.14.6|{hashlib.sha256(manifest).hexdigest().upper()}"
    with zipfile.ZipFile(destination) as archive:
        stamp_name = f"{package.name}/.venv/.elren-python-dependencies-v2"
        assert archive.read(stamp_name).decode("utf-8") == expected


@pytest.mark.parametrize(
    ("env_contents", "vault_contents", "error"),
    [
        ("DEEPSEEK_API_KEY=plaintext\n", b"ELRENVLT\x01encrypted", "credential field"),
        ("DEEPSEEK_API_KEY=\n", b'{"deepseek_primary":"plaintext"}', "not an encrypted"),
    ],
)
def test_private_archive_rejects_plaintext_credential_material(
    tmp_path: Path, env_contents: str, vault_contents: bytes, error: str
) -> None:
    package = tmp_path / "package-v1.0-private"
    (package / "data").mkdir(parents=True)
    (package / ".env").write_text(env_contents, encoding="utf-8")
    (package / "data/provider-secrets.vault").write_bytes(vault_contents)
    (package / "README.md").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        build(package, tmp_path / "private.zip", include_private_keys=True)


@pytest.mark.parametrize(
    "runtime_settings",
    [
        '{"deepseek_primary":"plaintext"}',
        '{"providers":[{"name":"custom","api_key":"plaintext"}]}',
    ],
)
def test_private_archive_rejects_credentials_in_runtime_settings(
    tmp_path: Path,
    runtime_settings: str,
) -> None:
    package = tmp_path / "package-v1.0-private"
    (package / "data").mkdir(parents=True)
    (package / ".env").write_text("ELREN_PORT=8765\n", encoding="utf-8")
    (package / "data/provider-secrets.vault").write_bytes(b"ELRENVLT\x01encrypted")
    (package / "data/runtime-settings.json").write_text(
        runtime_settings,
        encoding="utf-8",
    )
    (package / "README.md").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="plaintext credential field"):
        build(package, tmp_path / "private.zip", include_private_keys=True)


def test_private_archive_keys_do_not_disable_portable_venv_rewrite(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-private"
    scripts = package / ".venv/Scripts"
    scripts.mkdir(parents=True)
    (package / ".venv/pyvenv.cfg").write_text(
        "home = C:\\Users\\Builder\\Python\n"
        "include-system-site-packages = false\n"
        "version = 3.14.6\n",
        encoding="utf-8",
    )
    (scripts / "activate.bat").write_text(
        "set VIRTUAL_ENV=C:\\Users\\Builder\\project\\.venv", encoding="utf-8"
    )
    (scripts / "helper.py").write_text(
        "#!C:\\Users\\Builder\\Python\\python.exe\nprint('ok')\n", encoding="utf-8"
    )
    (package / ".env").write_text(
        "DEEPSEEK_API_KEY=\nELREN_PORT=8765\n", encoding="utf-8"
    )

    destination = tmp_path / "private-portable.zip"
    build(package, destination, include_private_keys=True)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        root = package.name
        assert archive.read(f"{root}/.venv/pyvenv.cfg").decode("utf-8") == (
            "include-system-site-packages = false\nversion = 3.14.6\n"
        )
        assert f"{root}/.venv/Scripts/activate.bat" not in names
        assert archive.read(f"{root}/.venv/Scripts/helper.py").decode("utf-8") == (
            "#!python\nprint('ok')\n"
        )
        assert f"{root}/.env" in names


def test_public_archive_removes_machine_paths_from_portable_venv(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    site_packages = package / ".venv/Lib/site-packages"
    scripts = package / ".venv/Scripts"
    metadata = site_packages / "elren_agent-1.0.0.dist-info"
    metadata.mkdir(parents=True)
    scripts.mkdir(parents=True)
    (package / ".venv/pyvenv.cfg").write_text(
        "home = C:\\Users\\Builder\\Python\n"
        "include-system-site-packages = false\n"
        "version = 3.14.6\n"
        "executable = C:\\Users\\Builder\\Python\\python.exe\n",
        encoding="utf-8",
    )
    (scripts / "activate.bat").write_text(
        "set VIRTUAL_ENV=C:\\Users\\Builder\\project\\.venv", encoding="utf-8"
    )
    (scripts / "helper.py").write_text(
        "#!C:\\Users\\Builder\\Python\\python.exe\nprint('ok')\n", encoding="utf-8"
    )
    (scripts / "pip.exe").write_bytes(
        b"distlib-launcher\0#!C:\\Users\\Builder\\Python\\python.exe\r\n"
    )
    (scripts / "python.exe").write_bytes(b"portable-venv-python")
    (site_packages / "_editable_impl_elren_agent.pth").write_text(
        "C:\\Users\\Builder\\project", encoding="utf-8"
    )
    (metadata / "direct_url.json").write_text(
        '{"url":"file:///C:/Users/Builder/project"}', encoding="utf-8"
    )
    (package / "README.md").write_text("portable", encoding="utf-8")

    destination = tmp_path / "release.zip"
    build(package, destination)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        config = archive.read(f"{package.name}/.venv/pyvenv.cfg").decode("utf-8")
        assert config == "include-system-site-packages = false\nversion = 3.14.6\n"
        assert f"{package.name}/.venv/Scripts/activate.bat" not in names
        assert archive.read(f"{package.name}/.venv/Scripts/helper.py").decode(
            "utf-8"
        ) == "#!python\nprint('ok')\n"
        assert f"{package.name}/.venv/Scripts/pip.exe" not in names
        assert archive.read(f"{package.name}/.venv/Scripts/python.exe") == (
            b"portable-venv-python"
        )
        assert f"{package.name}/.venv/Lib/site-packages/_editable_impl_elren_agent.pth" not in names
        assert f"{package.name}/.venv/Lib/site-packages/elren_agent-1.0.0.dist-info/direct_url.json" not in names


def test_public_archive_rejects_credentials_in_env_template(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    package.mkdir()
    (package / ".env.example").write_text(
        "DEEPSEEK_API_KEY=sk-must-not-ship\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="non-empty credential"):
        build(package, tmp_path / "unsafe.zip")


def test_public_archive_rejects_machine_local_first_party_paths(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    package.mkdir()
    clipboard_path = (
        "C:"
        + "\\Users\\Builder\\AppData\\Local\\Temp\\"
        + "codex-clipboard-private.png"
    )
    (package / "design-qa.md").write_text(clipboard_path, encoding="utf-8")

    with pytest.raises(ValueError, match="machine-local clipboard path"):
        build(package, tmp_path / "unsafe.zip")

    (package / "design-qa.md").write_text(
        f"local build root: {Path.home().as_posix()}/private-workspace",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="absolute home path"):
        build(package, tmp_path / "unsafe-home.zip")


@pytest.mark.parametrize(
    "entry",
    [
        "AWS_ACCESS_KEY_ID=must-not-ship\n",
        "ELREN_SESSION_COOKIE=must-not-ship\n",
        "OUTBOUND_WEBHOOK_URL=https://example.invalid/private\n",
    ],
)
def test_public_archive_rejects_additional_credential_shapes(
    tmp_path: Path, entry: str
) -> None:
    package = tmp_path / "package-v1.0-public"
    package.mkdir()
    (package / ".env.example").write_text(entry, encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty credential"):
        build(package, tmp_path / "unsafe.zip")


def test_release_archive_is_reproducible_across_source_mtime_changes(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package-v1.0-public"
    nested = package / "deepdesk"
    nested.mkdir(parents=True)
    (package / "README.md").write_text("portable", encoding="utf-8")
    (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    build(package, first)
    for path in (package, nested, package / "README.md", nested / "module.py"):
        os.utime(path, (2_000_000_000, 2_000_000_000))
    build(package, second)

    assert first.read_bytes() == second.read_bytes()


def test_release_archive_rejects_allowed_symbolic_links(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    (package / "deepdesk").mkdir(parents=True)
    private_target = tmp_path / "outside-secret.txt"
    private_target.write_text("must not cross the package boundary", encoding="utf-8")
    link = package / "deepdesk" / "linked-secret.txt"
    try:
        link.symlink_to(private_target)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    with pytest.raises(ValueError, match="symbolic links"):
        build(package, tmp_path / "unsafe.zip")


def test_release_archive_materializes_internal_pnpm_directory_links(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package-v1.0-public"
    modules = package / "work/openclaw-runtime/node_modules"
    target = modules / ".pnpm/demo@1.0.0/node_modules/demo"
    target.mkdir(parents=True)
    (target / "index.js").write_text("export const ready = true;\n", encoding="utf-8")
    payload = (target / "index.js").read_bytes()
    (target / "package.json").write_text(
        '{"name":"demo","version":"1.0.0"}\n',
        encoding="utf-8",
    )
    link = modules / "demo"
    core_packages = (
        (
            modules / ".pnpm/openclaw@1.0.0/node_modules/openclaw",
            modules / "openclaw",
            "openclaw.mjs",
        ),
        (
            modules
            / ".pnpm/modelcontextprotocol-sdk@1.0.0/node_modules/@modelcontextprotocol/sdk",
            modules / "@modelcontextprotocol/sdk",
            "package.json",
        ),
        (
            modules / ".pnpm/zod@4.0.0/node_modules/zod",
            modules / "zod",
            "package.json",
        ),
    )
    for core_target, _core_link, entry_name in core_packages:
        core_target.mkdir(parents=True)
        (core_target / entry_name).write_bytes(b"runtime entry\n")
    node_runtime = package / "work/node-runtime"
    (node_runtime / "node_modules/corepack/shims").mkdir(parents=True)
    (node_runtime / "node.exe").write_bytes(b"portable-node")
    (node_runtime / "npm.cmd").write_text("@echo off\n", encoding="utf-8")
    (node_runtime / "node_modules/corepack/shims/pnpm.cmd").write_text(
        "@echo off\n", encoding="utf-8"
    )
    try:
        link.symlink_to(
            Path(os.path.relpath(target, link.parent)),
            target_is_directory=True,
        )
        for core_target, core_link, _entry_name in core_packages:
            core_link.parent.mkdir(parents=True, exist_ok=True)
            core_link.symlink_to(
                Path(os.path.relpath(core_target, core_link.parent)),
                target_is_directory=True,
            )
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    build(package, first)
    os.utime(target / "index.js", (2_000_000_000, 2_000_000_000))
    build(package, second)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        root = package.name
        alias = f"{root}/work/openclaw-runtime/node_modules/demo/index.js"
        stored = (
            f"{root}/work/openclaw-runtime/node_modules/"
            ".pnpm/demo@1.0.0/node_modules/demo/index.js"
        )
        assert archive.read(alias) == payload
        assert stored not in archive.namelist()
        assert not any(
            "/work/openclaw-runtime/node_modules/.pnpm/" in name.casefold()
            for name in archive.namelist()
        )
        assert archive.read(f"{root}/work/node-runtime/node.exe") == b"portable-node"
        assert f"{root}/work/node-runtime/npm.cmd" in archive.namelist()
        assert (
            f"{root}/work/node-runtime/node_modules/corepack/shims/pnpm.cmd"
            in archive.namelist()
        )
        for _core_target, core_link, entry_name in core_packages:
            core_entry = (
                Path(root)
                / "work/openclaw-runtime/node_modules"
                / core_link.relative_to(modules)
                / entry_name
            ).as_posix()
            assert archive.read(core_entry) == b"runtime entry\n"
            assert (archive.getinfo(core_entry).external_attr >> 16) & 0o170000 != 0o120000
        assert (archive.getinfo(alias).external_attr >> 16) & 0o170000 != 0o120000


def test_release_archive_rejects_pnpm_link_outside_runtime_root(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package-v1.0-public"
    modules = package / "work/openclaw-runtime/node_modules"
    modules.mkdir(parents=True)
    qa_data = package / "qa-artifacts/runtime-secret"
    qa_data.mkdir(parents=True)
    (qa_data / "secret.txt").write_text("must not ship", encoding="utf-8")
    link = modules / "escaped"
    try:
        link.symlink_to(qa_data, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    with pytest.raises(ValueError, match="remain inside OpenClaw node_modules"):
        build(package, tmp_path / "unsafe.zip")


def test_release_archive_rejects_recursive_internal_pnpm_links(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package-v1.0-public"
    modules = package / "work/openclaw-runtime/node_modules"
    package_a = modules / ".pnpm/a@1.0.0/node_modules/a"
    package_b = modules / ".pnpm/b@1.0.0/node_modules/b"
    (package_a / "node_modules").mkdir(parents=True)
    (package_b / "node_modules").mkdir(parents=True)
    link_to_b = package_a / "node_modules/b"
    link_to_a = package_b / "node_modules/a"
    root_link = modules / "a"
    try:
        root_link.symlink_to(
            Path(os.path.relpath(package_a, root_link.parent)),
            target_is_directory=True,
        )
        link_to_b.symlink_to(
            Path(os.path.relpath(package_b, link_to_b.parent)),
            target_is_directory=True,
        )
        link_to_a.symlink_to(
            Path(os.path.relpath(package_a, link_to_a.parent)),
            target_is_directory=True,
        )
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    with pytest.raises(ValueError, match="link cycle"):
        build(package, tmp_path / "unsafe.zip")


def test_release_archive_rejects_pnpm_link_outside_trusted_store(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package-v1.0-public"
    modules = package / "work/openclaw-runtime/node_modules"
    target = modules / "unpacked-dependency"
    target.mkdir(parents=True)
    link = modules / "alias"
    try:
        link.symlink_to(
            Path(os.path.relpath(target, link.parent)),
            target_is_directory=True,
        )
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    with pytest.raises(ValueError, match="trusted .pnpm store"):
        build(package, tmp_path / "unsafe.zip")


def test_release_archive_rejects_broken_pnpm_links(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    modules = package / "work/openclaw-runtime/node_modules"
    modules.mkdir(parents=True)
    link = modules / "missing"
    try:
        link.symlink_to(
            Path(".pnpm/missing@1.0.0/node_modules/missing"),
            target_is_directory=True,
        )
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    with pytest.raises(ValueError, match="broken or cyclic"):
        build(package, tmp_path / "unsafe.zip")


def test_release_archive_rejects_pnpm_file_links(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    modules = package / "work/openclaw-runtime/node_modules"
    target = modules / ".pnpm/demo@1.0.0/node_modules/demo/package.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"name":"demo"}\n', encoding="utf-8")
    link = modules / "demo.json"
    try:
        link.symlink_to(Path(os.path.relpath(target, link.parent)))
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    with pytest.raises(ValueError, match="target directories"):
        build(package, tmp_path / "unsafe.zip")


def test_release_link_guard_also_recognizes_windows_junctions() -> None:
    class JunctionLike:
        @staticmethod
        def is_symlink() -> bool:
            return False

        @staticmethod
        def is_junction() -> bool:
            return True

    assert is_filesystem_link(JunctionLike()) is True  # type: ignore[arg-type]


def test_release_archive_rejects_destination_inside_source_tree(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    package.mkdir()
    (package / "README.md").write_text("portable", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the source tree"):
        build(package, package / "release.zip")


def test_public_package_sanitizer_removes_private_state_and_refuses_other_roots(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package-v1.0-public"
    (package / "data").mkdir(parents=True)
    (package / "outputs").mkdir()
    (package / ".venv/Lib/site-packages/elren_agent-1.0.0.dist-info").mkdir(
        parents=True
    )
    (package / "skills/example/.ruff_cache").mkdir(parents=True)
    (package / "skills/example/.ruff_cache/binary-entry").write_bytes(b"\x8e\x96")
    (package / "mobile/ElrenMobile/app/build").mkdir(parents=True)
    (package / "mobile/ElrenMobile/.gradle-corrupt-20260813").mkdir(parents=True)
    (package / "work/studio").mkdir(parents=True)
    (package / "work/sandbox").mkdir(parents=True)
    (package / "work/qa-workspace").mkdir(parents=True)
    (package / "work/openclaw-runtime/node_modules/.ignored_openclaw").mkdir(parents=True)
    (package / "work/openclaw-runtime/node_modules/.ignored").mkdir(parents=True)
    (package / "work/openclaw-runtime/node_modules/.bin").mkdir(parents=True)
    (package / "work/openclaw-runtime/node_modules/.pnpm/cache-index").mkdir(
        parents=True
    )
    (package / "work/openclaw-runtime/node_modules/.pnpm/cache-index/state.json").write_text(
        '{"cache":true}', encoding="utf-8"
    )
    (package / ".venv/Scripts").mkdir(parents=True)
    (package / ".venv/Scripts/helper.py").write_text(
        "#!C:\\Users\\Builder\\Python\\python.exe\nprint('ok')\n", encoding="utf-8"
    )
    (package / ".venv/Scripts/pygrun").write_text(
        "#!C:\\Users\\Builder\\Python\\python.exe\nprint('ok')\n", encoding="utf-8"
    )
    (package / ".venv/Scripts/pip.exe").write_bytes(
        b"distlib-launcher\0#!C:\\Users\\Builder\\Python\\python.exe\r\n"
    )
    (package / ".venv/Scripts/python.exe").write_bytes(b"portable-venv-python")
    (package / "work/openclaw-runtime/skills-check.json").write_text(
        '{"workspaceDir":"C:\\\\Users\\\\Builder\\\\.openclaw\\\\workspace"}',
        encoding="utf-8",
    )
    (package / "work/openclaw-runtime/node_modules/.bin/openclaw").write_text(
        "#!/bin/sh\n# cmd-shim-target=C:\\Users\\Builder\\openclaw.mjs\n",
        encoding="utf-8",
    )
    (package / "work/openclaw-runtime/node_modules/.modules.yaml").write_text(
        '{"nodeLinker":"hoisted","storeDir":"C:\\\\Users\\\\Builder\\\\pnpm-store",'
        '"virtualStoreDir":"C:\\\\Users\\\\Builder\\\\package\\\\node_modules\\\\.pnpm"}',
        encoding="utf-8",
    )
    (package / "work/openclaw-runtime/node_modules/.pnpm-workspace-state-v1.json").write_text(
        '{"projects":{"C:\\\\Users\\\\Builder\\\\package":{"name":"sidecar"}}}',
        encoding="utf-8",
    )
    (package / "work/browser-runtime/chromium").mkdir(parents=True)
    (package / "dist/package-v8-private/data").mkdir(parents=True)
    (package / "data/gateway.token").write_text("secret", encoding="utf-8")
    (package / "outputs/report.txt").write_text("private", encoding="utf-8")
    (package / ".env").write_text("SECRET=value", encoding="utf-8")
    (package / "mobile/ElrenMobile/local.properties").write_text(
        "sdk.dir=C:\\builder", encoding="utf-8"
    )
    (package / "work/studio/state.txt").write_text("stale", encoding="utf-8")
    (package / "work/sandbox/state.txt").write_text("volatile", encoding="utf-8")
    (package / "work/qa-workspace/deepdesk.db").write_bytes(b"history")
    (package / "work/openclaw-runtime/node_modules/.ignored_openclaw/old.js").write_text(
        "obsolete", encoding="utf-8"
    )
    (package / "work/openclaw-runtime/node_modules/.ignored/old.js").write_text(
        "obsolete", encoding="utf-8"
    )
    (package / "work/browser-runtime/chromium/debug.log").write_text(
        "machine-local browser log", encoding="utf-8"
    )
    (package / "work/openclaw-runtime/upstream-debug.pdb").write_bytes(
        b"C:\\Users\\BuildAgent\\AppData\\Local\\Temp\\compiler-state"
    )
    (package / "dist/package-v8-private/data/provider-keys.json").write_text(
        "secret", encoding="utf-8"
    )
    (package / "README.md").write_text("keep", encoding="utf-8")
    (package / ".env.example").write_text(
        "DEEPSEEK_API_KEY=\nDEEPSEEK_BASE_URL=https://api.deepseek.com\n",
        encoding="utf-8",
    )
    (package / "memory").mkdir()
    (package / "memory/private.md").write_text("private", encoding="utf-8")
    (package / "USER.md").write_text("private profile", encoding="utf-8")
    (package / "unrelated.txt").write_text("private", encoding="utf-8")

    removed = sanitize(package)

    assert removed
    assert not (package / ".env").exists()
    assert list((package / "data").iterdir()) == []
    assert list((package / "outputs").iterdir()) == []
    assert not (package / "skills/example/.ruff_cache").exists()
    assert not (package / "mobile/ElrenMobile/app/build").exists()
    assert not (package / "mobile/ElrenMobile/.gradle-corrupt-20260813").exists()
    assert not (package / "work/studio").exists()
    assert not (package / "work/sandbox").exists()
    assert not (package / "work/qa-workspace").exists()
    assert not (package / "work/openclaw-runtime/node_modules/.ignored_openclaw").exists()
    assert not (package / "work/openclaw-runtime/node_modules/.ignored").exists()
    assert (package / ".venv/Scripts/helper.py").read_text(encoding="utf-8") == (
        "#!python\nprint('ok')\n"
    )
    assert (package / ".venv/Scripts/pygrun").read_text(encoding="utf-8") == (
        "#!python\nprint('ok')\n"
    )
    assert not (package / ".venv/Scripts/pip.exe").exists()
    assert (package / ".venv/Scripts/python.exe").read_bytes() == b"portable-venv-python"
    assert not (package / "work/openclaw-runtime/skills-check.json").exists()
    assert not (package / "work/openclaw-runtime/node_modules/.bin").exists()
    assert not (package / "work/openclaw-runtime/node_modules/.pnpm").exists()
    assert not (
        package / "work/openclaw-runtime/node_modules/.pnpm-workspace-state-v1.json"
    ).exists()
    modules_state = (
        package / "work/openclaw-runtime/node_modules/.modules.yaml"
    ).read_text(encoding="utf-8")
    assert '"nodeLinker": "hoisted"' in modules_state
    assert "storeDir" not in modules_state
    assert "virtualStoreDir" not in modules_state
    assert "C:\\Users\\Builder" not in modules_state
    assert not (package / "work/browser-runtime/chromium/debug.log").exists()
    assert not (package / "work/openclaw-runtime/upstream-debug.pdb").exists()
    assert not (package / "dist").exists()
    assert not (package / "mobile/ElrenMobile/local.properties").exists()
    assert (package / "README.md").read_text(encoding="utf-8") == "keep"
    assert (package / ".env.example").is_file()
    assert not (package / "memory").exists()
    assert not (package / "USER.md").exists()
    assert not (package / "unrelated.txt").exists()

    wrong = tmp_path / "package-v1.0-private"
    wrong.mkdir()
    with pytest.raises(ValueError):
        sanitize(wrong)


def test_public_sanitizer_rejects_credentials_in_env_template(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    package.mkdir()
    (package / ".env.example").write_text(
        "ELREN_TELEGRAM_BOT_TOKEN=private-token\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="non-empty credential"):
        sanitize(package)


def test_public_sanitizer_rejects_machine_local_first_party_paths(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package-v1.0-public"
    package.mkdir()
    clipboard_path = (
        "C:"
        + "/Users/Builder/AppData/Local/Temp/"
        + "codex-clipboard-private.png"
    )
    (package / "design-qa.md").write_text(clipboard_path, encoding="utf-8")

    with pytest.raises(ValueError, match="machine-local clipboard path"):
        sanitize(package)


def test_public_package_sanitizer_rejects_symbolic_links(tmp_path: Path) -> None:
    package = tmp_path / "package-v1.0-public"
    (package / "deepdesk").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = package / "deepdesk" / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    with pytest.raises(ValueError, match="symbolic links"):
        sanitize(package)


def test_source_release_documentation_retains_honest_publication_gates() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    notes = Path("RELEASE-NOTES.md").read_text(encoding="utf-8")

    assert "private chat is not left visible" not in readme.casefold()
    assert "隐私聊天继续停留" not in readme
    assert "No final public ZIP is represented as built" not in notes
    normalized_notes = " ".join(notes.split())
    assert "v272-agent-fix" in notes
    assert "not represented as commercially signed, notarized, legally approved" in normalized_notes
    assert "owner-private archives must never be published" in normalized_notes
    assert "does not establish a reasoning-quality improvement" in normalized_notes
    assert "do not substitute for final archive inspection" in normalized_notes
    assert "Public-distribution gates still open" in notes
    assert "no top-level project LICENSE file" in notes
    assert "notice gaps" in notes
