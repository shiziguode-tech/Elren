"""Task-local state for the package-local Audiveris Java process.

Setting user.home alone does not relocate Audiveris on Windows. Supply all
platform directory contracts explicitly; language weights may be shared read-only.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from deepdesk.audiveris_tessdata import validate_directory
from deepdesk.subprocess_env import credential_safe_environment

_BOOTSTRAP_DIRECTORY = Path(__file__).resolve().parent / "java"
_BOOTSTRAP_SOURCE_SHA256 = "e8e856454d5caf09e67314992058b590bb666de8228ae24fc14b8800dc8a1279"
_BOOTSTRAP_JAR_SHA256 = "9f92acb6e1b7c7df11bf9bcd14f225802cbde32612032125220e44c60de3db4e"


def audiveris_bootstrap_args(audiveris_home: Path) -> list[str]:
    """Validated helper classpath/main arguments, after JVM options, before CLI args.

    Both reviewed source and precompiled Java 17 bytecode ship as package data.
    A missing/changed helper is an installation error, never a host-path fallback.
    """
    source = _BOOTSTRAP_DIRECTORY / "ElrenAudiverisBootstrap.java"
    jar = _BOOTSTRAP_DIRECTORY / "audiveris-bootstrap.jar"
    try:
        source_digest = hashlib.sha256(source.read_text(encoding="utf-8").encode()).hexdigest()
        jar_digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    except OSError as error:
        raise RuntimeError("Bundled Audiveris directory bootstrap is missing") from error
    if source_digest != _BOOTSTRAP_SOURCE_SHA256 or jar_digest != _BOOTSTRAP_JAR_SHA256:
        raise RuntimeError("Bundled Audiveris directory bootstrap failed integrity verification")
    app = audiveris_home.resolve(strict=True) / "app"
    if not app.is_dir():
        raise RuntimeError("Bundled Audiveris application directory is missing")
    return ["--add-opens=java.desktop/javax.swing.filechooser=ALL-UNNAMED",
            "-cp", str(jar) + os.pathsep + str(app / "*"),
            "org.elren.omr.ElrenAudiverisBootstrap"]


def audiveris_environment(job_root: Path, *, tessdata: Path | None = None) -> tuple[dict[str, str], list[str]]:
    state = job_root.resolve(strict=True) / "runtime-state"
    locations = {
        "HOME": state,
        "USERPROFILE": state,
        "APPDATA": state / "AppData/Roaming",
        "LOCALAPPDATA": state / "AppData/Local",
        "XDG_CONFIG_HOME": state / "config",
        "XDG_CACHE_HOME": state / "cache",
        "XDG_DATA_HOME": state / "data",
        "TEMP": state / "tmp",
        "TMP": state / "tmp",
        "TMPDIR": state / "tmp",
    }
    cache = state / "javacpp-cache"
    empty_tessdata = state / "tessdata"
    # Java's Windows FileSystemView also resolves these known folders while
    # initializing WellKnowns, even for a headless batch process.
    for location in {*locations.values(), cache, empty_tessdata, state / "Desktop",
                     state / "Documents", state / "AppData/Local/Microsoft/Windows/Fonts"}:
        location.mkdir(parents=True, exist_ok=True)
    # Never silently consume a host TESSDATA_PREFIX if the package is missing
    # its reviewed language data. The caller reports incomplete OCR separately.
    languages = tessdata.resolve(strict=True) if tessdata is not None else empty_tessdata
    if not languages.is_dir():
        raise ValueError("Audiveris language data must be a directory")
    language_argument = str(languages)
    if os.name == "nt" and not language_argument.isascii():
        # Tesseract's native Windows directory enumeration cannot reliably use
        # UTF-8 absolute paths. Java can use a Unicode cwd; keep the native
        # argument ASCII and relative to that exact task-local cwd instead.
        if empty_tessdata.resolve(strict=True) != empty_tessdata:
            raise ValueError("Redirected task-local language directory")
        if tessdata is not None:
            validate_directory(languages)
            for source in languages.iterdir():
                with source.open("rb") as src, (empty_tessdata / source.name).open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            validate_directory(empty_tessdata)
        language_argument = "runtime-state/tessdata"
    environment = credential_safe_environment()
    for name in list(environment):
        if name.upper() in {*locations, "TESSDATA_PREFIX", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH"}:
            del environment[name]
    environment.update({name: str(location) for name, location in locations.items()})
    environment.update(TESSDATA_PREFIX=language_argument, ELREN_OMR_OFFLINE="1")
    options = [f"-Duser.home={state}", f"-Djava.io.tmpdir={locations['TEMP']}",
               f"-Dorg.bytedeco.javacpp.cachedir={cache}",
               f"-Delren.omr.state.root={state}", "-Djava.awt.headless=true"]
    return environment, options
