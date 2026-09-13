from __future__ import annotations

from copy import deepcopy

from deepdesk.display_text import (
    add_task_display_overrides,
    normalize_assistant_display_text,
)


def test_preserves_historical_unquoted_shape_without_roundtrip_proof() -> None:
    ambiguous = r"第一段\n第二段\n第三段\n第四段"

    assert normalize_assistant_display_text(ambiguous) == ambiguous


def test_decodes_exact_whole_field_json_string_envelopes() -> None:
    encoded_lf = r'"第一段\n第二段\n第三段"'
    encoded_crlf = r'"first\r\nsecond\r\nthird"'

    assert normalize_assistant_display_text(encoded_lf) == "第一段\n第二段\n第三段"
    assert normalize_assistant_display_text(encoded_crlf) == "first\r\nsecond\r\nthird"


def test_requires_an_exact_json_roundtrip() -> None:
    assert normalize_assistant_display_text(' "first\\nsecond"') == ' "first\\nsecond"'
    assert normalize_assistant_display_text(r'"first\u000asecond"') == r'"first\u000asecond"'


def test_preserves_single_and_double_escaped_tokens() -> None:
    assert normalize_assistant_display_text(r"字符 \n 保持字面含义") == r"字符 \n 保持字面含义"
    encoded_twice = r'"first\\nsecond\\nthird\\nfourth"'
    assert normalize_assistant_display_text(encoded_twice) == encoded_twice


def test_preserves_code_fences_and_inline_code() -> None:
    normal_markdown = "说明\n```python\nvalue = r'a\\nb'\n```\n结束"
    fully_escaped_code = r'"说明\n```python\nvalue = r\u0027a\\nb\u0027\n```\n结束"'
    inline_code = r'"Use `a\nb` here\nThen continue\nAnd finish"'

    assert normalize_assistant_display_text(normal_markdown) == normal_markdown
    assert normalize_assistant_display_text(fully_escaped_code) == fully_escaped_code
    assert normalize_assistant_display_text(inline_code) == inline_code


def test_preserves_regex_windows_paths_and_json_documents() -> None:
    regex = r'"^foo\\nbar\\nbaz$\nqux"'
    windows_path = r'"C:\\new\\reports\nready"'
    json_document = r'"{\"value\":\"a\\nb\"}\n"'

    assert normalize_assistant_display_text(regex) == regex
    assert normalize_assistant_display_text(windows_path) == windows_path
    assert normalize_assistant_display_text(json_document) == json_document


def test_api_overrides_are_sparse_and_keep_raw_structured_data() -> None:
    raw = r'"第一段\n第二段\n第三段\n第四段"'
    payload = {
        "result": raw,
        "events": [
            {"type": "assistant", "data": {"content": raw}},
            {
                "type": "team_member_report",
                "data": {"content": raw, "model": "example"},
            },
            {
                "type": "tool_call",
                "data": {
                    "arguments": {
                        "command": r'python -c "print(\"a\\nb\")"',
                        "path": r"C:\new\reports",
                    }
                },
            },
        ],
    }
    original = deepcopy(payload)

    rendered = add_task_display_overrides(payload)

    assert rendered["result"] == original["result"]
    assert rendered["display_result"] == "第一段\n第二段\n第三段\n第四段"
    assert rendered["events"][0]["data"]["content"] == raw
    assert rendered["events"][0]["data"]["display_content"] == rendered["display_result"]
    assert rendered["events"][1]["data"]["content"] == raw
    assert rendered["events"][1]["data"]["display_content"] == rendered["display_result"]
    assert rendered["events"][2] == original["events"][2]


def test_api_override_is_absent_for_unambiguous_normal_text() -> None:
    payload = {
        "result": "already\nrendered",
        "events": [{"type": "assistant", "data": {"content": "already\nrendered"}}],
    }

    rendered = add_task_display_overrides(payload)

    assert "display_result" not in rendered
    assert "display_content" not in rendered["events"][0]["data"]
