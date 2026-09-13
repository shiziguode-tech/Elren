"""Check actual Audiveris directory policy, not just environment construction."""
from __future__ import annotations

import hashlib
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from deepdesk.audiveris_environment import audiveris_bootstrap_args, audiveris_environment

ROOT = Path(__file__).resolve().parents[1]


def test_host_configuration_is_not_inherited(tmp_path, monkeypatch):
    for name in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH",
                 "TESSDATA_PREFIX", "APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE",
                 "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, "synthetic-host-must-not-be-used")
    monkeypatch.setenv("API_SECRET", "synthetic-not-for-the-child")
    environment, options = audiveris_environment(tmp_path)
    assert "API_SECRET" not in environment
    assert all("synthetic-host-must-not-be-used" not in value for value in environment.values())
    assert all(name not in environment for name in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH"))
    for name in ("APPDATA", "LOCALAPPDATA", "HOME", "USERPROFILE", "XDG_CONFIG_HOME",
                 "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMP", "TEMP", "TMPDIR", "TESSDATA_PREFIX"):
        assert Path(environment[name]).is_relative_to(tmp_path)
        assert Path(environment[name]).is_dir()
    assert len(options) == 5
    assert "-Djava.awt.headless=true" in options


def test_shared_language_data_does_not_share_mutable_state(tmp_path):
    languages = tmp_path / "reviewed-languages"
    languages.mkdir()
    results = []
    for name in ("task-one", "task-two"):
        job = tmp_path / name
        job.mkdir()
        environment, options = audiveris_environment(job, tessdata=languages)
        assert environment["TESSDATA_PREFIX"] == str(languages)
        assert Path(environment["APPDATA"]).is_relative_to(job)
        results.append((environment, options))
    assert results[0][0]["APPDATA"] != results[1][0]["APPDATA"]
    assert results[0][1] != results[1][1]


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path contract")
def test_unicode_model_path_uses_verified_task_local_relative_data(tmp_path, monkeypatch):
    import deepdesk.audiveris_environment as module
    languages = tmp_path / "中文模型"
    languages.mkdir()
    (languages / "eng.traineddata").write_bytes(b"synthetic pinned bytes")
    checked = []
    monkeypatch.setattr(module, "validate_directory", lambda path: checked.append(path) or path)
    job = tmp_path / "中文任务"
    job.mkdir()
    environment, _ = module.audiveris_environment(job, tessdata=languages)
    assert environment["TESSDATA_PREFIX"] == "runtime-state/tessdata"
    copied = job / environment["TESSDATA_PREFIX"]
    assert (copied / "eng.traineddata").read_bytes() == b"synthetic pinned bytes"
    assert checked == [languages, copied]


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path contract")
def test_unicode_model_copy_refuses_overwrite(tmp_path, monkeypatch):
    import deepdesk.audiveris_environment as module
    languages = tmp_path / "中文模型"
    languages.mkdir()
    (languages / "eng.traineddata").write_bytes(b"new")
    monkeypatch.setattr(module, "validate_directory", lambda path: path)
    target = tmp_path / "job/runtime-state/tessdata"
    target.mkdir(parents=True)
    existing = target / "eng.traineddata"
    existing.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        module.audiveris_environment(tmp_path / "job", tessdata=languages)
    assert existing.read_bytes() == b"preserve"


@pytest.mark.parametrize("filename", ["ElrenAudiverisBootstrap.java", "audiveris-bootstrap.jar"])
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_bootstrap_missing_or_changed_fails_closed(tmp_path, monkeypatch, filename, damage):
    import deepdesk.audiveris_environment as module

    for file in module._BOOTSTRAP_DIRECTORY.iterdir():
        if file.is_file():
            (tmp_path / file.name).write_bytes(file.read_bytes())
    target = tmp_path / filename
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(target.read_bytes() + b"\n// QA changed\n")
    monkeypatch.setattr(module, "_BOOTSTRAP_DIRECTORY", tmp_path)
    with pytest.raises(RuntimeError, match="bootstrap"):
        audiveris_bootstrap_args(tmp_path)


def test_source_checkout_line_endings_do_not_break_integrity(tmp_path, monkeypatch):
    import deepdesk.audiveris_environment as module

    (tmp_path / "app").mkdir()
    for file in module._BOOTSTRAP_DIRECTORY.iterdir():
        if file.suffix == ".java":
            (tmp_path / file.name).write_bytes(file.read_text(encoding="utf-8").replace("\n", "\r\n").encode())
        elif file.is_file():
            (tmp_path / file.name).write_bytes(file.read_bytes())
    monkeypatch.setattr(module, "_BOOTSTRAP_DIRECTORY", tmp_path)
    assert audiveris_bootstrap_args(tmp_path)[-1] == "org.elren.omr.ElrenAudiverisBootstrap"


def test_release_source_collectors_include_both_bootstrap_resources(tmp_path):
    from launcher.build_macos_source_archive import iter_macos_source_paths
    from launcher.build_release_archive import iter_release_paths

    relative_files = {Path("deepdesk/java") / name for name in
                      ("ElrenAudiverisBootstrap.java", "audiveris-bootstrap.jar")}
    for relative in relative_files:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    for collector in (iter_macos_source_paths, iter_release_paths):
        actual = {relative for path, relative in collector(tmp_path) if path.is_file()}
        assert relative_files <= actual
    assert '--add-data "$ROOT/deepdesk/java:deepdesk/java"' in (
        ROOT / "macos/build-macos-app.sh").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bootstrap_fixture(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("Real private Windows JVM fixture")
    directory = tmp_path_factory.mktemp("bootstrap-fixture")
    java = ROOT / "work/tool-runtime/native/audiveris/runtime/bin/java.exe"
    javac = Path("C:/Program Files/Android/Android Studio/jbr/bin/javac.exe")
    assert java.is_file() and javac.is_file()
    env, _ = audiveris_environment(directory)
    classes = directory / "classes"
    classes.mkdir()
    source = ROOT / "deepdesk/java/ElrenAudiverisBootstrap.java"
    # Recompile source independently and byte-for-byte verify the shipped JAR:
    # runtime must never require javac, or accept a stale compiled helper.
    result = subprocess.run([str(javac), "--release", "17", "-encoding", "UTF-8", "-g:none",
                             "-d", str(classes), str(source)], env=env,
                            capture_output=True, text=True, timeout=30, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    jar = directory / "rebuilt.jar"
    with zipfile.ZipFile(jar, "w", compression=zipfile.ZIP_STORED) as output:
        for file in sorted(classes.rglob("*.class")):
            info = zipfile.ZipInfo(file.relative_to(classes).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            output.writestr(info, file.read_bytes())
    assert jar.read_bytes() == source.with_name("audiveris-bootstrap.jar").read_bytes()
    (directory / "rebuilt.sha256").write_text(hashlib.sha256(jar.read_bytes()).hexdigest())
    # This target has the same fixed class name, but no real business/OMR work.
    fixture_source = ROOT / "tests/audiveris_bootstrap_fixture/Audiveris.java"
    target = directory / "target"
    target.mkdir()
    result = subprocess.run([str(javac), "--release", "17", "-d", str(target), str(fixture_source)],
                            env=env, capture_output=True, text=True, timeout=30, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stdout + result.stderr
    return java, jar, target


@pytest.mark.parametrize("mode", ["success", "target-failure", "missing-state", "wrong-home",
                                  "not-headless", "missing-module-open", "missing-documents"])
def test_real_bootstrap_calls_fixed_main_only_after_valid_policy(tmp_path, bootstrap_fixture, mode):
    java, jar, target = bootstrap_fixture
    env, options = audiveris_environment(tmp_path)
    extra = ["--add-opens=java.desktop/javax.swing.filechooser=ALL-UNNAMED"]
    args = ["-batch", "a path with spaces", "原谱"]
    if mode == "target-failure":
        args = ["fail"]
    elif mode == "missing-state":
        options = [item for item in options if not item.startswith("-Delren.omr.state.root=")]
    elif mode == "wrong-home":
        options = [item for item in options if not item.startswith("-Duser.home=")]
        options.append(f"-Duser.home={tmp_path}")
    elif mode == "not-headless":
        options = [item for item in options if item != "-Djava.awt.headless=true"]
        options.append("-Djava.awt.headless=false")
    elif mode == "missing-module-open":
        extra = []
    elif mode == "missing-documents":
        (tmp_path / "runtime-state/Documents").rmdir()
    result = subprocess.run([str(java), *options, *extra, "-Dfile.encoding=UTF-8",
                             "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",
                             "-cp", str(jar) + os.pathsep + str(target),
                             "org.elren.omr.ElrenAudiverisBootstrap", *args],
                            cwd=tmp_path, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=15, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    (tmp_path / "bootstrap.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if mode == "success":
        assert result.returncode == 0, result.stdout + result.stderr
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if line.startswith("QA_"))
        assert [values[f"QA_ARG_{index}"] for index in range(3)] == args
        assert Path(values["QA_DEFAULT"]) == tmp_path / "runtime-state/Documents"
        assert Path(values["QA_HOME"]) == tmp_path / "runtime-state"
    else:
        assert result.returncode != 0
        if mode == "target-failure":
            assert "QA_TARGET_REACHED=true" in result.stdout
            assert "QA_ORIGINAL_FAILURE" in result.stderr
            assert "InvocationTargetException" not in result.stderr
        else:
            assert "QA_TARGET_REACHED" not in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Tests Windows Audiveris directory selection using real bundled Java")
def test_real_audiveris_uses_each_task_profile(tmp_path):
    runtime = ROOT / "work/tool-runtime/native/audiveris"
    java = runtime / "runtime/bin/java.exe"
    javac = Path("C:/Program Files/Android/Android Studio/jbr/bin/javac.exe")
    assert java.is_file() and javac.is_file(), "Real package JVM and local probe compiler required"
    source = ROOT / "tests/AudiverisStateProbe.java"
    classes = tmp_path / "classes"
    classes.mkdir()
    compilation_env, _ = audiveris_environment(tmp_path)
    bootstrap = audiveris_bootstrap_args(runtime)
    compiled = subprocess.run([str(javac), "--release", "17", "-cp", bootstrap[2],
                               "-d", str(classes), str(source)],
                              env=compilation_env, capture_output=True, text=True, errors="replace",
                              timeout=30, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    configurations = []
    for name in ("first", "second"):
        job = tmp_path / name
        job.mkdir()
        environment, options = audiveris_environment(job)
        result = subprocess.run([str(java), *options, "-Djava.awt.headless=true",
                                 "--enable-native-access=ALL-UNNAMED",
                                 "--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED",
                                 bootstrap[0], "-cp", str(classes) + os.pathsep + bootstrap[2],
                                 "AudiverisStateProbe"], cwd=job, env=environment,
                                capture_output=True, text=True, errors="replace", timeout=30,
                                check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        (job / "probe.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if line.startswith("QA_"))
        config = Path(values["QA_CONFIG_FOLDER"])
        assert len(values) == 8
        assert all(Path(value).is_relative_to(job) for value in values.values()), values
        configurations.append(config)
    assert configurations[0] != configurations[1]
