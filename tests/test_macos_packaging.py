from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import plistlib
import runpy
import struct
import sys
import types
import zipfile
from pathlib import Path

import pytest

from deepdesk import engine
from deepdesk.macos_ocr import MacOSOCR
from deepdesk.plugins.builtin.macos_ui import MacOSUITool
from launcher.build_macos_source_archive import (
    ARCHIVE_ROOT,
)
from launcher.build_macos_source_archive import (
    build as build_macos_source_archive,
)
from macos import bundle_audiveris, check_ocr_models

ROOT = Path(__file__).parents[1]


def test_native_menu_uses_standard_responder_chain_without_clipboard_access():
    source = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    menu = source.split("func makeElrenMainMenu", 1)[1].split("final class AppDelegate", 1)[0]
    assert "NSApp.mainMenu = makeElrenMainMenu" in source
    for action in ("NSText.cut", "NSText.copy", "NSText.paste", "NSText.selectAll", "NSApplication.terminate"):
        assert action in menu
    assert 'NSSelectorFromString("undo:")' in menu
    assert 'NSSelectorFromString("redo:")' in menu
    assert "[.command, .shift]" in menu
    assert "NSPasteboard" not in menu
    assert ".target =" not in menu


def test_local_new_tab_and_artifact_links_do_not_replace_chat_window():
    source = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    helper = source.split("func shouldOpenLocalLinkExternally", 1)[1].split("final class", 1)[0]
    assert "guard linkActivated else { return false }" in helper
    for route in ('"/api/artifacts/"', '"/api/uploads/"', '"/screenshots/"'):
        assert route in helper
    policy = source.split("decidePolicyFor navigationAction", 1)[1].split("func makeElrenMainMenu", 1)[0]
    assert policy.index("port == allowedPort") < policy.index("shouldOpenLocalLinkExternally(")
    assert "newWindow: navigationAction.targetFrame == nil" in policy
    assert "linkActivated: navigationAction.navigationType == .linkActivated" in policy


def test_full_macos_release_requires_bundled_node_before_build_cleanup() -> None:
    script = (ROOT / "macos/build-macos-app.sh").read_text("utf-8")
    assert script.index("Full releases must carry Node/OpenClaw") < script.index('mkdir -p "$CONTENTS/MacOS"')
    assert 'rm -rf "$BUILD"' not in script
    assert "Refusing to overwrite a previous build or archive" in script
    assert "refusing an incomplete release" in script
    assert "OpenClaw will use a compatible system installation" not in script


def test_macos_ui_passes_arguments_through_jxa_run_without_interpolation(monkeypatch) -> None:
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return types.SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")
    monkeypatch.setattr("deepdesk.plugins.builtin.macos_ui.sys.platform", "darwin")
    monkeypatch.setattr("deepdesk.plugins.builtin.macos_ui.subprocess.run", run)
    name = 'App "name"; not JavaScript'
    assert MacOSUITool()._execute_sync({"action": "activate", "application": name}) == {"ok": True}
    assert calls[0][-1] == name
    script = calls[0][-2]
    assert script.startswith("function run(argv) {")
    assert "Application(argv[0])" in script
    assert "return JSON.stringify" in script
    assert name not in script
    assert "arguments[" not in script


def test_macos_frozen_entry_routes_metadata_without_importing_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    worker = types.ModuleType("deepdesk.score_title_ocr")
    worker.main = lambda arguments: calls.append(arguments) or 7
    monkeypatch.setitem(sys.modules, "deepdesk.score_title_ocr", worker)
    monkeypatch.setattr(sys, "argv", ["ElrenBackend", "--score-metadata", "/local/第三首.pdf"])
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "deepdesk.main":
            raise AssertionError("OCR worker must never import/start the service")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as finished:
        runpy.run_path(str(ROOT / "macos/backend_entry.py"), run_name="__main__")
    assert finished.value.code == 7
    assert calls == [["/local/第三首.pdf"]]


@pytest.mark.parametrize("arguments", [
    ["--score-metadata"], ["--score-metadata", ""],
    ["--score-metadata", "one.pdf", "two.pdf"],
    ["-m", "deepdesk.score_title_ocr", "one.pdf"],
    ["--unknown"],
])
def test_macos_frozen_entry_rejects_invalid_modes_without_starting_any_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["ElrenBackend", *arguments])
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"deepdesk.main", "deepdesk.score_title_ocr"}:
            raise AssertionError("Invalid arguments must not initialize any backend")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as finished:
        runpy.run_path(str(ROOT / "macos/backend_entry.py"), run_name="__main__")
    assert finished.value.code == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_macos_frozen_entry_without_arguments_still_starts_service_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    server = types.ModuleType("deepdesk.main")
    server.run = lambda: calls.append("run")
    monkeypatch.setitem(sys.modules, "deepdesk.main", server)
    monkeypatch.setattr(sys, "argv", ["ElrenBackend"])
    with pytest.raises(SystemExit) as finished:
        runpy.run_path(str(ROOT / "macos/backend_entry.py"), run_name="__main__")
    assert finished.value.code == 0
    assert calls == ["run"]


@pytest.fixture
def reviewed_macos_omr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Synthetic structures only: never launch Java or download an app."""
    source = tmp_path / "Audiveris.app/Contents"
    app = source / "app"
    runtime = source / "runtime/Contents/Home"
    app.mkdir(parents=True)
    for directory in ("bin", "lib/server", "legal/java.base"):
        (runtime / directory).mkdir(parents=True, exist_ok=True)
    macho = b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + b"\0" * 24
    (runtime / "bin/java").write_bytes(macho)
    (runtime / "bin/java").chmod(0o755)
    (runtime / "lib/server/libjvm.dylib").write_bytes(macho)
    (runtime / "lib/modules").write_bytes(b"synthetic Java modules")
    (runtime / "release").write_text('JAVA_VERSION="25.0.3"\n', "utf-8")
    (runtime / "legal/java.base/LICENSE").write_text("Synthetic Java legal notice", "utf-8")
    (app / "Audiveris.cfg").write_text("java-options=-Djpackage.app-version=5.11.0\n", "utf-8")
    model = io.BytesIO()
    with zipfile.ZipFile(model, "w") as archive:
        for name in ("model.xml", "means.xml", "stds.xml"):
            archive.writestr(name, "<synthetic-test-model/>")
    model_data = model.getvalue()
    monkeypatch.setattr(bundle_audiveris, "MODEL_SHA256", hashlib.sha256(model_data).hexdigest())
    with zipfile.ZipFile(app / "audiveris.jar", "w") as jar:
        jar.writestr("META-INF/MANIFEST.MF", "Implementation-Version: 5.11.0\r\n")
        jar.writestr("Audiveris.class", b"\xca\xfe\xba\xbe\0\0" + struct.pack(">H", 69))
        jar.writestr(bundle_audiveris.MODEL_ENTRY, model_data)
    for component in ("leptonica", "tesseract"):
        with zipfile.ZipFile(app / f"{component}-1-macosx-arm64.jar", "w") as jar:
            jar.writestr(f"org/test/lib{component}.dylib", macho)
    return source


def test_macos_build_requires_and_connects_complete_offline_omr() -> None:
    script = (ROOT / "macos/build-macos-app.sh").read_text("utf-8")
    installer = (ROOT / "launcher/install-jianpu-runtime-macos.sh").read_text("utf-8")
    launcher = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    assert script.index("--check-only --forbid-root") < script.index('mkdir -p "$CONTENTS/MacOS"')
    assert 'ELREN_AUDIVERIS_SOURCE:-/Applications/Audiveris.app/Contents' in script
    assert '"$AUDIVERIS_SOURCE" "$RESOURCES/bundle"' in script
    assert 'exec "$python_bin" "$project_root/macos/bundle_audiveris.py"' in installer
    assert 'environment["ELREN_AUDIVERIS_HOME"] = resources' in launcher
    assert 'bundle/work/tool-runtime/native/audiveris' in launcher
    assert script.index('--refresh-manifest') < script.index('codesign --force --options runtime')
    assert 'codesign --force --deep' not in script
    assert script.rindex('--verify-installed') > script.index('codesign --verify --deep --strict')
    assert "'Mach-O'" in script
    assert '"$executable" == "$AUDIVERIS_HOME/runtime/bin/java"' in script
    assert 'elif [[ "${executable:t}" == "node" ]]; then' in script
    assert '--entitlements "$ROOT/macos/Backend.entitlements" "$executable"' in script
    backend_entitlements = plistlib.loads((ROOT / "macos/Backend.entitlements").read_bytes())
    assert backend_entitlements["com.apple.security.cs.disable-library-validation"] is True
    assert "com.apple.security.get-task-allow" not in backend_entitlements
    node_signing = script.split('elif [[ "${executable:t}" == "node" ]]; then', 1)[1].split("    else", 1)[0]
    assert '--entitlements "$ROOT/macos/Elren.entitlements" "$executable"' in node_signing
    node_entitlements = plistlib.loads((ROOT / "macos/Elren.entitlements").read_bytes())
    assert node_entitlements["com.apple.security.cs.allow-jit"] is True
    assert node_entitlements["com.apple.security.cs.allow-unsigned-executable-memory"] is True
    assert "com.apple.security.get-task-allow" not in node_entitlements
    assert 'elif [[ "${executable:t}" == "chrome-headless-shell" ]]; then' in script
    chrome_signing = script.split('elif [[ "${executable:t}" == "chrome-headless-shell" ]]; then', 1)[1].split("    else", 1)[0]
    assert '--entitlements "$ROOT/macos/Chromium.entitlements" "$executable"' in chrome_signing
    chrome_entitlements = plistlib.loads((ROOT / "macos/Chromium.entitlements").read_bytes())
    assert chrome_entitlements["com.apple.security.cs.allow-jit"] is True
    assert chrome_entitlements["com.apple.security.cs.disable-library-validation"] is True
    assert "com.apple.security.get-task-allow" not in chrome_entitlements
    entitlements = plistlib.loads((ROOT / "macos/Audiveris.entitlements").read_bytes())
    assert entitlements["com.apple.security.cs.allow-jit"] is True
    assert entitlements["com.apple.security.cs.disable-library-validation"] is True
    assert script.index('python "$ROOT/macos/check_ocr_models.py"') < script.index('python -m PyInstaller')
    assert script.index('--backend "$RESOURCES/backend"') > script.index('cp -R "$BUILD/pyinstaller/ElrenBackend/."')


def test_macos_metadata_models_must_be_present_and_survive_freezing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "rapidocr"
    frozen = tmp_path / "backend/_internal/rapidocr"
    for package in (original, frozen):
        (package / "models").mkdir(parents=True)
        for name in check_ocr_models.MODELS:
            (package / "models" / name).write_bytes(b"synthetic local model " + name.encode())
    monkeypatch.setattr(check_ocr_models, "MODEL_SHA256", {
        name: hashlib.sha256(b"synthetic local model " + name.encode()).hexdigest()
        for name in check_ocr_models.MODELS
    })
    check_ocr_models.verify_frozen_models(original, tmp_path / "backend")
    (frozen / "models" / check_ocr_models.MODELS[0]).write_bytes(b"incorrect model")
    with pytest.raises(ValueError, match="SHA-256"):
        check_ocr_models.verify_frozen_models(original, tmp_path / "backend")
    (original / "models" / check_ocr_models.MODELS[0]).write_bytes(b"incorrect model")
    with pytest.raises(ValueError, match="SHA-256"):
        check_ocr_models.verify_frozen_models(original, tmp_path / "backend")
    (original / "models" / check_ocr_models.MODELS[0]).write_bytes(
        b"synthetic local model " + check_ocr_models.MODELS[0].encode()
    )
    (original / "models" / check_ocr_models.MODELS[1]).unlink()
    with pytest.raises(ValueError, match="missing"):
        check_ocr_models.model_hashes(original)


@pytest.mark.parametrize("invalid_state", ["missing", "empty", "corrupt"])
def test_macos_metadata_model_rejection_does_not_import_ocr_or_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_state: str,
) -> None:
    package = tmp_path / "rapidocr"
    (package / "models").mkdir(parents=True)
    fake_hashes = {}
    for name in check_ocr_models.MODELS:
        data = b"synthetic reviewed model " + name.encode()
        (package / "models" / name).write_bytes(data)
        fake_hashes[name] = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(check_ocr_models, "MODEL_SHA256", fake_hashes)
    target = package / "models" / check_ocr_models.MODELS[0]
    if invalid_state == "missing":
        target.unlink()
    else:
        target.write_bytes(b"" if invalid_state == "empty" else b"corrupted model")
    monkeypatch.setattr(check_ocr_models.importlib.util, "find_spec", lambda name: types.SimpleNamespace(origin=str(package / "__init__.py")))
    monkeypatch.setattr(sys, "argv", ["check_ocr_models.py"])
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "rapidocr" or name.startswith("rapidocr."):
            raise AssertionError("Validation must not import OCR or its downloader")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as finished:
        check_ocr_models.main()
    assert finished.value.code == 2


@pytest.mark.parametrize("nested_runtime", [True, False])
def test_offline_macos_omr_install_normalizes_and_verifies(
    reviewed_macos_omr: Path, tmp_path: Path, nested_runtime: bool,
) -> None:
    if not nested_runtime:
        (reviewed_macos_omr / "runtime/Contents/Home").rename(reviewed_macos_omr / "runtime-flat")
        (reviewed_macos_omr / "runtime/Contents").rmdir()
        (reviewed_macos_omr / "runtime").rmdir()
        (reviewed_macos_omr / "runtime-flat").rename(reviewed_macos_omr / "runtime")
    workspace = tmp_path / "bundle"
    workspace.mkdir()
    installed = bundle_audiveris.install(
        reviewed_macos_omr, workspace, ROOT / "launcher/audiveris-AGPL-3.0-LICENSE.txt",
    )
    assert installed == workspace / bundle_audiveris.RELATIVE_HOME
    assert (installed / "runtime/bin/java").is_file()
    assert not (installed / "runtime/Contents").exists()
    assert (installed / "runtime/legal/java.base/LICENSE").is_file()
    assert (installed / "model/basic-classifier.zip").is_file()
    bundle_audiveris.verify(installed)
    manifest = json.loads((installed / bundle_audiveris.MANIFEST).read_text("utf-8"))
    assert manifest["platform"] == "darwin-arm64"
    assert manifest["cloud_upload"] is False
    assert any(row["path"] == "runtime/bin/java" for row in manifest["files"])
    assert any(row["path"] == "licenses/audiveris-AGPL-3.0.txt" for row in manifest["files"])


@pytest.mark.parametrize("missing", [
    "app/audiveris.jar", "app/Audiveris.cfg", "runtime/Contents/Home/bin/java",
    "runtime/Contents/Home/lib/server/libjvm.dylib", "runtime/Contents/Home/lib/modules",
    "runtime/Contents/Home/release", "runtime/Contents/Home/legal/java.base/LICENSE",
    "app/tesseract-1-macosx-arm64.jar", "app/leptonica-1-macosx-arm64.jar",
])
def test_offline_macos_omr_rejects_missing_components_without_partial_install(
    reviewed_macos_omr: Path, tmp_path: Path, missing: str,
) -> None:
    (reviewed_macos_omr / missing).unlink()
    with pytest.raises((ValueError, OSError)):
        bundle_audiveris.install(
            reviewed_macos_omr, tmp_path, ROOT / "launcher/audiveris-AGPL-3.0-LICENSE.txt",
        )
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("bad_component", ["version", "java", "jvm", "java_version", "model", "license", "native_jar"])
def test_offline_macos_omr_rejects_unreviewed_payload(
    reviewed_macos_omr: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_component: str,
) -> None:
    license_path = ROOT / "launcher/audiveris-AGPL-3.0-LICENSE.txt"
    runtime = reviewed_macos_omr / "runtime/Contents/Home"
    if bad_component == "version":
        (reviewed_macos_omr / "app/Audiveris.cfg").write_text("jpackage.app-version=5.11.01", "utf-8")
    elif bad_component in {"java", "jvm"}:
        (runtime / ("bin/java" if bad_component == "java" else "lib/server/libjvm.dylib")).write_bytes(b"MZ Windows binary")
    elif bad_component == "java_version":
        (runtime / "release").write_text('JAVA_VERSION="17.0.1"\n', "utf-8")
    elif bad_component == "model":
        monkeypatch.setattr(bundle_audiveris, "MODEL_SHA256", "0" * 64)
    elif bad_component == "license":
        license_path = tmp_path / "invalid-license.txt"
        license_path.write_text("wrong license", "utf-8")
    else:
        with zipfile.ZipFile(reviewed_macos_omr / "app/tesseract-1-macosx-arm64.jar", "w") as jar:
            jar.writestr("libtesseract.dylib", b"MZ Windows library")
    with pytest.raises(ValueError):
        bundle_audiveris.install(reviewed_macos_omr, tmp_path, license_path)
    assert not (tmp_path / "work").exists()


def test_offline_macos_omr_never_overwrites_and_hashes_post_signing(
    reviewed_macos_omr: Path, tmp_path: Path,
) -> None:
    license_path = ROOT / "launcher/audiveris-AGPL-3.0-LICENSE.txt"
    target = bundle_audiveris.install(reviewed_macos_omr, tmp_path, license_path)
    before = (target / bundle_audiveris.MANIFEST).read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        bundle_audiveris.install(reviewed_macos_omr, tmp_path, license_path)
    assert (target / bundle_audiveris.MANIFEST).read_bytes() == before
    with (target / "runtime/bin/java").open("ab") as signed:
        signed.write(b"synthetic signature bytes")
    with pytest.raises(ValueError, match="integrity"):
        bundle_audiveris.verify(target)
    bundle_audiveris.verify(target, refresh_manifest=True)
    bundle_audiveris.verify(target)
    (target / "model/basic-classifier.zip").write_bytes(b"changed model")
    with pytest.raises(ValueError, match="classifier"):
        bundle_audiveris.verify(target, refresh_manifest=True)


def test_offline_macos_omr_rejects_external_link(
    reviewed_macos_omr: Path, tmp_path: Path,
) -> None:
    external = tmp_path / "private.txt"
    external.write_text("must not be copied", "utf-8")
    link = reviewed_macos_omr / "app/leak.txt"
    try:
        link.symlink_to(external)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")
    with pytest.raises(ValueError, match="escapes"):
        bundle_audiveris.install(reviewed_macos_omr, tmp_path, ROOT / "launcher/audiveris-AGPL-3.0-LICENSE.txt")
    assert not (tmp_path / "work").exists()


def test_offline_macos_omr_manifest_verifies_metadata_and_nested_names(
    reviewed_macos_omr: Path, tmp_path: Path,
) -> None:
    target = bundle_audiveris.install(reviewed_macos_omr, tmp_path, ROOT / "launcher/audiveris-AGPL-3.0-LICENSE.txt")
    manifest_path = target / bundle_audiveris.MANIFEST
    original = manifest_path.read_text("utf-8")
    payload = json.loads(original)
    payload["platform"] = "win32"
    manifest_path.write_text(json.dumps(payload), "utf-8")
    with pytest.raises(ValueError, match="integrity"):
        bundle_audiveris.verify(target)
    manifest_path.write_text(original, "utf-8")
    (target / "app" / bundle_audiveris.MANIFEST).write_text("unrecorded extra file", "utf-8")
    with pytest.raises(ValueError, match="integrity"):
        bundle_audiveris.verify(target)


def test_offline_macos_omr_copies_internal_legal_link_as_regular_file(
    reviewed_macos_omr: Path, tmp_path: Path,
) -> None:
    legal = reviewed_macos_omr / "runtime/Contents/Home/legal"
    (legal / "java.logging").mkdir()
    link = legal / "java.logging/LICENSE"
    try:
        link.symlink_to(Path("..") / "java.base" / "LICENSE")
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")
    target = bundle_audiveris.install(reviewed_macos_omr, tmp_path, ROOT / "launcher/audiveris-AGPL-3.0-LICENSE.txt")
    assert not (target / "runtime/legal/java.logging/LICENSE").is_symlink()
    assert (target / "runtime/legal/java.logging/LICENSE").read_bytes() == link.read_bytes()


def test_offline_macos_omr_rejects_redirected_destination(
    reviewed_macos_omr: Path, tmp_path: Path,
) -> None:
    external = tmp_path / "untouched"
    external.mkdir()
    workspace = tmp_path / "bundle"
    workspace.mkdir()
    try:
        (workspace / "work").symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")
    with pytest.raises(ValueError, match="redirected"):
        bundle_audiveris.install(reviewed_macos_omr, workspace, ROOT / "launcher/audiveris-AGPL-3.0-LICENSE.txt")
    assert not list(external.iterdir())


def test_macos_bundle_uses_native_webkit_and_owned_loopback_backend() -> None:
    source = (ROOT / "macos" / "ElrenMac.swift").read_text(encoding="utf-8")
    assert "import AppKit" in source
    assert "import WebKit" in source
    assert 'environment["ELREN_HOST"] = "127.0.0.1"' in source
    assert 'environment["ELREN_SKIP_AUTO_BROWSER"] = "1"' in source
    assert 'host == "127.0.0.1"' in source
    assert "port == allowedPort" in source
    assert 'url.scheme == "about"' in source
    assert 'url.isFileURL' not in source
    assert "navigationDelegate.allowedPort = port" in source
    assert "backend.terminate()" in source
    assert 'object["root_hash"] as? String == expectedIdentity' in source
    assert "attachToExistingOrStartBackend()" in source
    assert 'ProcessInfo.processInfo.environment["ELREN_PORT"]' in source
    assert 'appendingPathComponent(".env")' in source
    assert "while normalized.count > 1 && normalized.hasSuffix" in source
    # Only URLSession's data task is resumable; probeIdentity itself is not.
    assert source.count("}.resume()") == 1
    assert "healthProbeInFlight" in source
    assert "healthGeneration" in source
    assert "isTerminating" in source
    assert "probeInFlight: self.healthProbeInFlight" in source
    assert "guard !probeInFlight, now >= nextProbeAt else { return .wait }" in source


def test_macos_webkit_attaches_before_loading_and_recovers_without_content_capture() -> None:
    source = (ROOT / "macos" / "ElrenMac.swift").read_text(encoding="utf-8")
    assert "WKWebView(frame: .zero" not in source
    assert source.index("window.contentView = webView") < source.index("webView.loadHTMLString(loading")
    assert "webView.autoresizingMask = [.width, .height]" in source
    assert "webView.layoutSubtreeIfNeeded()" in source
    assert "webViewWebContentProcessDidTerminate" in source
    assert "guard contentRecoveryCount < 2" in source
    assert "didFailProvisionalNavigation" in source
    assert "NSURLErrorCancelled" in source
    assert "takeSnapshot" not in source


def test_macos_build_creates_standard_signed_bundle_and_optional_notarization() -> None:
    script = (ROOT / "macos" / "build-macos-app.sh").read_text(encoding="utf-8")
    assert 'CONTENTS="$APP/Contents"' in script
    assert '"$CONTENTS/MacOS/Elren"' in script
    assert "pyinstaller" in script
    assert "PLAYWRIGHT_BROWSERS_PATH" in script
    assert "playwright install --only-shell chromium" in script
    assert "xcrun swiftc" in script
    assert 'MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}"' in script
    assert '[[ "$ARCH" != "arm64" ]]' in script
    assert "sys.version_info[:2] != (3,12)" in script
    assert "Python 3.12 is required" in script
    assert '-target "${ARCH}-apple-macos${MACOSX_DEPLOYMENT_TARGET}"' in script
    assert "codesign --verify --deep --strict" in script
    assert "xcrun notarytool submit" in script
    assert "ditto -c -k --sequesterRsrc --keepParent" in script
    assert "work/tool-runtime/native/jianpu-ly" in script
    assert "jianpu_ly.py" in script
    assert "ELREN_JIANPU_LY_HOME" in (ROOT / "macos" / "ElrenMac.swift").read_text(
        encoding="utf-8"
    )
    runtime_package = (ROOT / "macos" / "runtime-package.json").read_text(encoding="utf-8")
    runtime_lock = (ROOT / "macos" / "runtime-package-lock.json").read_text(encoding="utf-8")
    python_lock = (ROOT / "macos" / "build-requirements.lock").read_text(encoding="utf-8")
    info_plist = (ROOT / "macos" / "Info.plist").read_text(encoding="utf-8")
    assert '"@hono/node-server": "2.1.0"' in runtime_package
    assert '"undici": "8.10.0"' in runtime_package
    assert '"fast-uri": "3.1.6"' in runtime_package
    assert '"qs": "6.16.0"' in runtime_package
    assert '"lockfileVersion": 3' in runtime_lock
    assert '"integrity": "sha512-' in runtime_lock
    assert "pip==26.2.1" in python_lock
    assert "pyinstaller==6.22.2" in python_lock
    assert "pytest==" in python_lock
    assert "ruff==" in python_lock
    assert "wheel==0.48.0" in python_lock
    locked_requirements = [
        line
        for line in python_lock.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert all("==" in requirement for requirement in locked_requirements)
    assert len(locked_requirements) == len(set(locked_requirements))
    # A clean source checkout has no generated/private work runtime. Verify
    # that the builder consumes the committed manifests, not a local install.
    assert 'RUNTIME_PACKAGE="$ROOT/macos/runtime-package.json"' in script
    assert 'RUNTIME_LOCK="$ROOT/macos/runtime-package-lock.json"' in script
    assert 'cp "$RUNTIME_PACKAGE" "$NODE_ROOT/package.json"' in script
    assert 'cp "$RUNTIME_LOCK" "$NODE_ROOT/package-lock.json"' in script
    assert "<string>14.0</string>" in info_plist
    assert "npm audit --omit=dev --audit-level=high" in script
    assert "npm ci --omit=dev --ignore-scripts" in script
    assert "-name .pnpm" in script
    assert "Refusing to package a pnpm virtual store" in script
    assert "pip install --upgrade" not in script
    assert '--requirement "$PYTHON_LOCK"' in script
    assert '--build-constraint "$PYTHON_LOCK"' in script
    assert "python -m pip check" in script
    assert "--paths \"$ROOT\"" in script
    assert "-name .ruff_cache" in script
    assert "22.22.3+, 24.15.0+, or 25.9.0+" in (ROOT / "macos" / "BUILD-ON-MAC.md").read_text(
        encoding="utf-8"
    )
    assert "command -v python3 2>/dev/null || true" in script
    assert "a===24" in script
    assert "a===25" in script


def test_macos_workflow_runs_quality_gates_before_build_and_upload() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-macos.yml").read_text(
        encoding="utf-8"
    )
    gates = workflow.index("- name: Quality gates")
    build = workflow.index("- name: Build Elren")
    upload = workflow.index("actions/upload-artifact@")

    assert gates < build < upload
    assert "macos/build-requirements.lock" in workflow
    assert "node-version: '22.22.3'" in workflow
    assert "python -m pip check" in workflow
    assert "python -m pytest -q" in workflow
    assert "python -m ruff check ." in workflow
    assert "node --check deepdesk/static/app.js" in workflow


def test_macos_dependencies_and_platform_tools_are_declared() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    main = (ROOT / "deepdesk" / "main.py").read_text(encoding="utf-8")
    assert "pywinauto>=0.6.9,<1; sys_platform == 'win32'" in project
    assert "pyobjc-framework-Vision" in project
    assert "MacOSUITool" in main
    assert "MacOSOCR" in main
    assert "PosixResourceSandbox" in main


def test_macos_agent_prompt_replaces_windows_only_tool_guidance(monkeypatch) -> None:
    monkeypatch.setattr(engine, "IS_MACOS", True)
    prompt = engine.system_prompt_for_current_platform()
    assert "Current operating system: macOS" in prompt
    assert "macos_ui" in prompt
    assert "Command+W" in prompt


def test_macos_capability_objects_fail_honestly_when_tested_on_windows() -> None:
    assert MacOSUITool().name == "macos_ui"
    # The adapter remains importable in Windows tests but never claims that
    # Apple Vision exists on the wrong operating system.
    import asyncio

    status = asyncio.run(MacOSOCR().status(probe=True))
    assert status["offline"] is True
    if not status["ready"]:
        assert status["error"]


def test_macos_source_archive_omits_personal_workspace_state(tmp_path: Path) -> None:
    source = tmp_path / "Elren-source"
    (source / "macos").mkdir(parents=True)
    (source / "memory").mkdir()
    (source / "README.md").write_text("public", encoding="utf-8")
    (source / "RELEASE-NOTES.md").write_text("v1.0 candidate", encoding="utf-8")
    (source / "THIRD_PARTY_NOTICES.md").write_text(
        "third-party inventory", encoding="utf-8"
    )
    (source / "macos/build-macos-app.sh").write_text("#!/bin/zsh", encoding="utf-8")
    for locked_input in (
        "build-requirements.in",
        "build-requirements.lock",
        "runtime-package.json",
        "runtime-package-lock.json",
    ):
        (source / "macos" / locked_input).write_text("locked", encoding="utf-8")
    (source / "macos/provider-keys.json").write_text("private", encoding="utf-8")
    (source / "macos/provider-secrets.vault").write_bytes(b"encrypted-private")
    (source / "macos/build.log").write_text("private path diagnostics", encoding="utf-8")
    (source / "memory/private.md").write_text("private", encoding="utf-8")
    (source / "USER.md").write_text("private profile", encoding="utf-8")
    (source / "openclaw-workspace-state.json").write_text("{}", encoding="utf-8")
    (source / "unrelated-draft.html").write_text("private draft", encoding="utf-8")
    (source / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    (source / "Elren.exe").write_bytes(b"windows launcher")
    (source / "Microsoft.Web.WebView2.Core.dll").write_bytes(b"windows runtime")
    nested_webview = source / "launcher" / "webview2"
    nested_webview.mkdir(parents=True)
    (nested_webview / "WebView2Loader.dll").write_bytes(b"nested windows runtime")
    (source / "start.ps1").write_text("Write-Host windows", encoding="utf-8")
    (source / "Elren.apk").write_bytes(b"android package")
    destination = tmp_path / "source.zip"

    build_macos_source_archive(source, destination)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert f"{ARCHIVE_ROOT}/README.md" in names
        assert f"{ARCHIVE_ROOT}/RELEASE-NOTES.md" in names
        assert f"{ARCHIVE_ROOT}/THIRD_PARTY_NOTICES.md" in names
        assert f"{ARCHIVE_ROOT}/macos/build-macos-app.sh" in names
        assert f"{ARCHIVE_ROOT}/macos/build-requirements.in" in names
        assert f"{ARCHIVE_ROOT}/macos/build-requirements.lock" in names
        assert f"{ARCHIVE_ROOT}/macos/runtime-package.json" in names
        assert f"{ARCHIVE_ROOT}/macos/runtime-package-lock.json" in names
        assert f"{ARCHIVE_ROOT}/macos/provider-keys.json" not in names
        assert f"{ARCHIVE_ROOT}/macos/provider-secrets.vault" not in names
        assert f"{ARCHIVE_ROOT}/macos/build.log" not in names
        assert f"{ARCHIVE_ROOT}/USER.md" not in names
        assert not any("/memory/" in name for name in names)
        assert f"{ARCHIVE_ROOT}/openclaw-workspace-state.json" not in names
        assert f"{ARCHIVE_ROOT}/unrelated-draft.html" not in names
        assert f"{ARCHIVE_ROOT}/.env.example" in names
        assert f"{ARCHIVE_ROOT}/Elren.exe" not in names
        assert f"{ARCHIVE_ROOT}/Microsoft.Web.WebView2.Core.dll" not in names
        assert f"{ARCHIVE_ROOT}/launcher/webview2/WebView2Loader.dll" not in names
        assert f"{ARCHIVE_ROOT}/start.ps1" not in names
        assert f"{ARCHIVE_ROOT}/Elren.apk" in names


def test_macos_source_archive_does_not_descend_into_excluded_trees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "Elren-source"
    blocked_directories = {
        source / "qa-artifacts",
        source / "work",
        source / "deepdesk" / "__pycache__",
        source / "plugins" / "demo" / "node_modules" / ".pnpm",
        source / "macos" / "build",
    }
    for directory in blocked_directories:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "must-not-be-visited.txt").write_text(
            "excluded", encoding="utf-8"
        )
    (source / "README.md").write_text("portable", encoding="utf-8")
    (source / "deepdesk" / "keep.py").write_text("KEEP = True\n", encoding="utf-8")
    (source / "macos" / "keep.sh").write_text("#!/bin/zsh\n", encoding="utf-8")

    original_scandir = os.scandir

    def guarded_scandir(path):
        candidate = Path(path)
        if candidate in blocked_directories:
            raise AssertionError(f"macOS traversal entered excluded directory: {path}")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    destination = tmp_path / "source.zip"
    build_macos_source_archive(source, destination)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
    assert f"{ARCHIVE_ROOT}/README.md" in names
    assert f"{ARCHIVE_ROOT}/deepdesk/keep.py" in names
    assert f"{ARCHIVE_ROOT}/macos/keep.sh" in names
    assert not any("must-not-be-visited.txt" in name for name in names)


def test_macos_source_archive_is_reproducible_across_mtime_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Elren-source"
    macos = source / "macos"
    macos.mkdir(parents=True)
    (source / "README.md").write_text("public", encoding="utf-8")
    script = macos / "build-macos-app.sh"
    script.write_text("#!/bin/zsh\n", encoding="utf-8")

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    build_macos_source_archive(source, first)
    for path in (source, macos, source / "README.md", script):
        os.utime(path, (2_000_000_000, 2_000_000_000))
    build_macos_source_archive(source, second)

    assert first.read_bytes() == second.read_bytes()


def test_macos_source_archive_rejects_allowed_symbolic_links(tmp_path: Path) -> None:
    source = tmp_path / "Elren-source"
    (source / "macos").mkdir(parents=True)
    private_target = tmp_path / "outside-secret.txt"
    private_target.write_text("private", encoding="utf-8")
    link = source / "macos" / "linked-secret.txt"
    try:
        link.symlink_to(private_target)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this host: {error}")

    with pytest.raises(ValueError, match="symbolic links"):
        build_macos_source_archive(source, tmp_path / "unsafe.zip")


def test_macos_source_archive_rejects_destination_inside_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "Elren-source"
    (source / "macos").mkdir(parents=True)

    with pytest.raises(ValueError, match="outside the source tree"):
        build_macos_source_archive(source, source / "source.zip")


def test_macos_source_archive_rejects_machine_local_first_party_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Elren-source"
    source.mkdir()
    clipboard_path = (
        "C:"
        + "/Users/Builder/AppData/Local/Temp/"
        + "codex-clipboard-private.png"
    )
    (source / "design-qa.md").write_text(clipboard_path, encoding="utf-8")

    with pytest.raises(ValueError, match="machine-local clipboard path"):
        build_macos_source_archive(source, tmp_path / "unsafe.zip")
