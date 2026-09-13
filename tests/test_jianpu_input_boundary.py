"""Validate the same character stream the pinned notation compiler interprets."""
from pathlib import Path

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.jianpu_omr import (
    MAX_JIANPU_SOURCE_BYTES,
    JianpuOMRTool,
    JianpuToStaffTool,
    _validate_jianpu_source,
)


def fullwidth(value):
    return "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in value)


@pytest.mark.parametrize("fragment", [
    "LP:\n% harmless raw-code marker\n:LP",
    "LPH:\n% harmless raw-header marker\n:LPH",
    '#(display "SYNTHETIC_COMPILER_MARKER")',
    '\\include "nonexistent-synthetic-fixture.ly"',
    '\\bookOutputName "synthetic-name"',
    '\\sourcefilename "synthetic-name"',
])
def test_fullwidth_cannot_bypass_notation_injection_guard(fragment):
    source = "1=C\n4/4\n" + fullwidth(fragment) + "\n1 2 3 4\n"
    with pytest.raises(ValueError, match="注入"):
        _validate_jianpu_source(source)


def test_fullwidth_standard_notation_compiles_without_changing_music():
    root = Path(__file__).resolve().parents[1]
    if JianpuOMRTool._jianpu_runtime(root) is None:
        pytest.skip("Pinned notation compiler unavailable")
    ascii_source = "1=C\n4/4\n1 2 3 4\n"
    fullwidth_source = fullwidth(ascii_source)
    normalized = _validate_jianpu_source(fullwidth_source)
    assert normalized == ascii_source
    compiled, _ = JianpuOMRTool._compile_jianpu_source(normalized, root)
    reference, _ = JianpuOMRTool._compile_jianpu_source(ascii_source, root)
    assert compiled == reference


@pytest.mark.parametrize("prefix", ["$", "$@", "#@"])
@pytest.mark.parametrize("wide", [False, True])
def test_alternate_scheme_entrypoints_are_rejected(prefix, wide):
    fragment = "L: " + prefix + "(+ 1 2)"
    source = "1=C\n4/4\n1 2 3 4\n" + (fullwidth(fragment) if wide else fragment)
    with pytest.raises(ValueError, match="注入"):
        _validate_jianpu_source(source)


@pytest.mark.parametrize("fragment", ["# [1 2]", "$`(1 ,(+ 1 2))", "#; ignored\n(+ 1 2)"])
def test_scheme_reader_variants_are_rejected(fragment):
    with pytest.raises(ValueError, match="注入"):
        _validate_jianpu_source("1=C\n4/4\n1 2 3 4\nL: " + fragment)


def test_safe_accidentals_lyrics_and_non_normalized_text_are_preserved():
    source = '1=C\n4/4\n#1 2 b3 4\nL: \\set stanza = #"1." "春²" "天" "秋" "夜"\n'
    assert _validate_jianpu_source(source) == source


def test_original_byte_limit_is_enforced_before_width_conversion():
    source = "1=C\n4/4\n" + "１" * (MAX_JIANPU_SOURCE_BYTES // 3 + 1)
    with pytest.raises(ValueError, match="4 MB"):
        _validate_jianpu_source(source)


@pytest.mark.asyncio
async def test_conversion_rejects_fullwidth_directive_before_creating_exports(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.jly"
    source.write_text("1=C\n4/4\n" + fullwidth("LP:\n#(+ 1 2)\n:LP") + "\n1 2 3 4\n", encoding="utf-8")

    def should_not_typeset(*args, **kwargs):
        pytest.fail("Rejected input reached the native typesetter")

    monkeypatch.setattr(JianpuOMRTool, "_typeset_jianpu_source", should_not_typeset)
    with pytest.raises(ValueError, match="注入"):
        await JianpuToStaffTool().execute(
            {"action": "convert", "input_path": str(source)},
            ToolContext(task_id="synthetic-boundary", workspace=str(tmp_path)),
        )
    assert not (tmp_path / "outputs").exists()
