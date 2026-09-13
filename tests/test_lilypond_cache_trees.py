"""Portable extraction must preserve both Guile and LilyPond bytecode use."""
import os

import pytest

from deepdesk.plugins.builtin import jianpu_omr


@pytest.mark.parametrize("trees", ["guile", "lilypond", "both"])
def test_portable_cache_repairs_both_distribution_trees_without_changing_bytes(tmp_path, trees):
    root = tmp_path / "lilypond-2.26.0"
    executable = root / "bin/lilypond.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic-executable-not-run")
    pairs = []
    if trees in {"guile", "both"}:
        pairs.append(("share/guile/3.0/ice-9/eval.scm", "lib/guile/3.0/ccache/ice-9/eval.go"))
    if trees in {"lilypond", "both"}:
        pairs.append(("share/lilypond/2.26.0/scm/lily/lily.scm", "lib/lilypond/2.26.0/ccache/lily/lily.go"))
    for source_name, cache_name in pairs:
        source, cache = root / source_name, root / cache_name
        source.parent.mkdir(parents=True, exist_ok=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"unchanged source")
        cache.write_bytes(b"unchanged compiled bytecode")
        os.utime(cache, (1_700_000_000, 1_700_000_000))
        os.utime(source, (1_700_000_010, 1_700_000_010))
    jianpu_omr.JianpuOMRTool._prepare_lilypond_cache(executable)
    for source_name, cache_name in pairs:
        source, cache = root / source_name, root / cache_name
        assert cache.stat().st_mtime_ns > source.stat().st_mtime_ns
        assert source.read_bytes() == b"unchanged source"
        assert cache.read_bytes() == b"unchanged compiled bytecode"
    stamps = {name: (root / name).stat().st_mtime_ns for pair in pairs for name in pair}
    jianpu_omr.JianpuOMRTool._prepare_lilypond_cache(executable)
    assert stamps == {name: (root / name).stat().st_mtime_ns for name in stamps}


def test_empty_runtime_does_not_create_or_compile_files(tmp_path):
    executable = tmp_path / "empty/bin/lilypond.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic")
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    jianpu_omr.JianpuOMRTool._prepare_lilypond_cache(executable)
    assert before == sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))


def test_newer_bytecode_is_not_retimestamped_when_sibling_is_stale(tmp_path):
    source = tmp_path / "share/lilypond/2.26.0/scm/lily/score.scm"
    stale = tmp_path / "lib/lilypond/2.26.0/ccache/lily/score.go"
    newer = stale.with_name("newer.go")
    source.parent.mkdir(parents=True)
    stale.parent.mkdir(parents=True)
    for path in (source, stale, newer):
        path.write_bytes(b"synthetic")
    os.utime(stale, (1_700_000_000, 1_700_000_000))
    os.utime(source, (1_700_000_010, 1_700_000_010))
    os.utime(newer, (1_700_000_100, 1_700_000_100))
    stamp = newer.stat().st_mtime_ns
    jianpu_omr.JianpuOMRTool._prepare_lilypond_cache(tmp_path / "bin/lilypond.exe")
    assert stale.stat().st_mtime_ns > source.stat().st_mtime_ns
    assert newer.stat().st_mtime_ns == stamp


def test_failed_timestamp_repair_is_not_marked_ready_and_can_retry(tmp_path, monkeypatch):
    source = tmp_path / "share/lilypond/2.26.0/scm/lily/score.scm"
    cache = tmp_path / "lib/lilypond/2.26.0/ccache/lily/score.go"
    source.parent.mkdir(parents=True)
    cache.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    cache.write_bytes(b"compiled")
    os.utime(cache, (1_700_000_000, 1_700_000_000))
    os.utime(source, (1_700_000_010, 1_700_000_010))
    original = os.utime

    def rejected(*args):
        raise PermissionError("synthetic read-only fixture")

    monkeypatch.setattr(jianpu_omr.os, "utime", rejected)
    executable = tmp_path / "bin/lilypond.exe"
    with pytest.raises(RuntimeError, match="缓存"):
        jianpu_omr.JianpuOMRTool._prepare_lilypond_cache(executable)
    assert tmp_path.resolve() not in jianpu_omr._PREPARED_LILYPOND_ROOTS
    assert cache.read_bytes() == b"compiled"
    monkeypatch.setattr(jianpu_omr.os, "utime", original)
    jianpu_omr.JianpuOMRTool._prepare_lilypond_cache(executable)
    assert cache.stat().st_mtime_ns > source.stat().st_mtime_ns
