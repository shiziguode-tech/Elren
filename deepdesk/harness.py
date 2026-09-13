from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from deepdesk.models import AgentProfile

_INTENT_TOOLS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(
            r"(?i)(简谱.{0,16}(?:转|转换|生成|导出).{0,12}五线谱|"
            r"(?:jianpu|numbered notation).{0,24}(?:to|into|convert).{0,12}staff)"
        ),
        ("jianpu_to_staff", "filesystem", "background_browser"),
    ),
    (
        re.compile(
            r"(?i)(五线谱.{0,16}(?:转|转换|识别|导出).{0,12}简谱|"
            r"staff.{0,24}(?:to|into|convert).{0,12}(?:jianpu|numbered notation))"
        ),
        ("jianpu_omr", "filesystem", "background_browser"),
    ),
    (
        re.compile(
            r"(?i)(control (?:my |the )?(?:pc|computer|desktop)|real desktop|visible desktop|"
            r"take over (?:my )?(?:pc|computer)|操控(?:我的|这台)?电脑|控制(?:我的|这台)?电脑|"
            r"接管(?:我的|这台)?电脑|真实点击|真实桌面)"
        ),
        ("live_computer_use", "windows_ui", "computer_use", "vision"),
    ),
    (
        re.compile(
            r"(?i)(code|coding|program|python|javascript|typescript|bug|fix|refactor|"
            r"test|repo|repository|代码|编程|程序|修复|重构|测试|项目|仓库)"
        ),
        ("filesystem", "shell", "sandbox", "process_manager", "background_browser"),
    ),
    (
        re.compile(r"(?i)(web|website|browser|html|css|dom|网页|浏览器|页面|界面|前端)"),
        ("background_browser", "vision", "filesystem", "shell"),
    ),
    (
        re.compile(r"(?i)(search|latest|current|news|research|搜索|联网|最新|当前|调研|核实)"),
        ("provider_web_search", "background_browser", "web"),
    ),
    (
        re.compile(r"(?i)(android|phone|mobile|手机|安卓|应用列表|扫码|绑定)"),
        ("mobile_device", "vision", "computer_use", "request_human_action"),
    ),
    (
        re.compile(r"(?i)(word|docx?|pdf|pptx?|excel|xlsx?|document|文档|表格|幻灯片)"),
        ("document", "filesystem", "shell"),
    ),
    (
        re.compile(r"(?i)(image|video|music|audio|图片|视频|音乐|音频|生成媒体)"),
        ("generate_media", "vision", "filesystem"),
    ),
    (
        re.compile(r"(?i)(settings?|configuration|设置|配置|默认模型|主题|密钥)"),
        ("update_settings", "filesystem"),
    ),
    (
        re.compile(r"(?i)(feishu|telegram|飞书|电报)"),
        ("feishu", "telegram", "filesystem"),
    ),
)

_LIVE_COMPUTER_USE_INTENT = re.compile(
    r"(?i)(control (?:my |the )?(?:pc|computer|desktop)|real (?:visible )?desktop|"
    r"take over (?:my )?(?:pc|computer)|use (?:my |the )?(?:mouse|keyboard)|"
    r"(?:click|type|drag|scroll).{0,24}(?:desktop|window|visible app|computer)|"
    r"操控(?:我的|这台)?电脑|控制(?:我的|这台)?电脑|接管(?:我的|这台)?电脑|"
    r"真实(?:点击|输入|拖动|滚动|操控)|"
    r"(?:点击|输入|拖动|滚动).{0,16}(?:电脑|桌面|窗口|界面|应用|软件))"
)

_PROFILE_TOOLS: dict[AgentProfile, tuple[str, ...]] = {
    AgentProfile.GENERAL: ("filesystem", "shell", "background_browser"),
    AgentProfile.PLANNER: ("filesystem", "provider_web_search", "background_browser"),
    AgentProfile.COMPUTER_USE: (
        "windows_ui",
        "live_computer_use",
        "computer_use",
        "vision",
        "computer",
    ),
    AgentProfile.CODER: ("filesystem", "shell", "sandbox", "background_browser"),
    AgentProfile.OFFICE: ("document", "filesystem", "windows_ui"),
    AgentProfile.GUARDIAN: ("filesystem", "provider_web_search", "vision"),
    AgentProfile.OPENCLAW: ("openclaw", "skills", "mcp"),
}

_GENERIC_TERMS = {
    "tool", "tools", "agent", "elren", "task", "file", "use", "using", "run",
    "工具", "任务", "功能", "使用", "进行", "这个", "那个", "问题", "帮我", "需要",
}

TOOL_SEARCH_NAME = "tool_search"
TOOL_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_SEARCH_NAME,
        "description": (
            "Find and activate additional registered Elren tools for this task. "
            "Use this when the needed capability is not already visible; no capability is removed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Capability or action to find, such as OCR, Telegram, cron, or phone control.",
                },
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional exact registered tool names to activate.",
                },
            },
            "additionalProperties": False,
        },
    },
}

DEFAULT_TOOL_SCHEMA_BUDGET = 18_000
DEFAULT_TOOL_SCHEMA_LIMIT = 12


def _terms(value: str) -> set[str]:
    lowered = str(value or "").casefold()
    words = set(re.findall(r"[a-z0-9_\-]{2,}", lowered))
    words.difference_update(_GENERIC_TERMS)
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", lowered))
    words.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    words.difference_update(_GENERIC_TERMS)
    return words


def recommended_tool_names(prompt: str, profile: AgentProfile) -> list[str]:
    """Return an ordered attention shortlist without removing any capability."""

    matched: list[str] = []
    for pattern, matched_names in _INTENT_TOOLS:
        if pattern.search(prompt):
            matched.extend(matched_names)
    profile_names = list(_PROFILE_TOOLS.get(profile, ()))
    # A conversion request must expose its dedicated deterministic converter
    # before broad profile defaults such as shell or vision. Other intents keep
    # the long-standing profile-first order.
    names = (
        [*matched, *profile_names]
        if "jianpu_omr" in matched or "jianpu_to_staff" in matched
        else [*profile_names, *matched]
    )
    names.extend(("request_human_action", "filesystem"))
    return list(dict.fromkeys(names))


def allows_live_computer_use_start(prompt: str, profile: AgentProfile | str) -> bool:
    """Host-side activation gate for the real foreground input capability."""

    try:
        normalized_profile = AgentProfile(profile)
    except (TypeError, ValueError):
        normalized_profile = None
    return normalized_profile == AgentProfile.COMPUTER_USE or bool(
        _LIVE_COMPUTER_USE_INTENT.search(str(prompt or ""))
    )


def rank_tool_schemas(
    schemas: Iterable[dict[str, Any]], prompt: str, profile: AgentProfile
) -> list[dict[str, Any]]:
    """Place likely tools first while retaining every registered schema unchanged."""

    preferred = recommended_tool_names(prompt, profile)
    preferred_index = {name: index for index, name in enumerate(preferred)}
    prompt_terms = _terms(prompt)

    def rank(schema: dict[str, Any]) -> tuple[int, int, str]:
        function = schema.get("function") or {}
        name = str(function.get("name") or "")
        if name in preferred_index:
            return (0, preferred_index[name], name)
        overlap = len(prompt_terms & _terms(f"{name} {function.get('description', '')}"))
        return (1, -overlap, name)

    return sorted(schemas, key=rank)


def select_tool_schemas(
    schemas: Iterable[dict[str, Any]],
    prompt: str,
    profile: AgentProfile,
    *,
    activated_names: Iterable[str] = (),
    character_budget: int = DEFAULT_TOOL_SCHEMA_BUDGET,
    tool_limit: int = DEFAULT_TOOL_SCHEMA_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return a focused initial catalog while keeping every tool discoverable.

    Large function catalogs measurably consume context and distract weaker
    models.  Relevant and explicitly activated schemas stay visible; the
    lightweight ``tool_search`` schema exposes every remaining registered tool
    on demand.  Small catalogs are returned unchanged.
    """

    ranked = rank_tool_schemas(list(schemas), prompt, profile)
    full_characters = len(json.dumps(ranked, ensure_ascii=False, default=str))
    if full_characters <= max(1_000, int(character_budget)):
        return ranked, {
            "deferred": False,
            "visible": len(ranked),
            "total": len(ranked),
            "full_schema_characters": full_characters,
            "visible_schema_characters": full_characters,
        }

    activated = {str(name) for name in activated_names if str(name).strip()}
    # Discovery must actually expose the requested capability, rather than
    # letting profile defaults consume its entire schema budget every turn.
    ranked.sort(key=lambda schema: str((schema.get('function') or {}).get('name') or '') not in activated)
    preferred = set(recommended_tool_names(prompt, profile)) | activated
    prompt_terms = _terms(prompt)
    selected: list[dict[str, Any]] = []
    selected_names: set[str] = set()
    selected_characters = len(json.dumps(TOOL_SEARCH_SCHEMA, ensure_ascii=False))
    bounded_limit = max(6, int(tool_limit))
    bounded_budget = max(6_000, int(character_budget))
    minimum_visible = min(6, bounded_limit)

    # First keep the profile/intent defaults and explicitly activated tools in
    # ranked order. Then fill spare slots only with actual semantic matches.
    # Alphabetical zero-overlap fillers made unrelated tools (for example a
    # mobile connector for a greeting) visible to the model and increased both
    # distraction and context usage without adding capability: every omitted
    # tool is still available through ``tool_search``.
    for preferred_only in (True, False):
        for schema in ranked:
            function = schema.get("function") or {}
            name = str(function.get("name") or "")
            if not name or name in selected_names:
                continue
            if preferred_only and name not in preferred:
                continue
            if not preferred_only and len(selected) >= minimum_visible:
                continue
            if not preferred_only and not (
                prompt_terms
                & _terms(f"{name} {function.get('description', '')}")
            ):
                continue
            encoded = len(json.dumps(schema, ensure_ascii=False, default=str))
            if selected and (
                len(selected) >= bounded_limit
                or selected_characters + encoded > bounded_budget
            ):
                continue
            selected.append(schema)
            selected_names.add(name)
            selected_characters += encoded

    visible = [TOOL_SEARCH_SCHEMA, *selected]
    return visible, {
        "deferred": True,
        "visible": len(selected),
        "total": len(ranked),
        "visible_names": [
            str((schema.get("function") or {}).get("name") or "") for schema in selected
        ],
        "deferred_names": [
            str((schema.get("function") or {}).get("name") or "")
            for schema in ranked
            if str((schema.get("function") or {}).get("name") or "") not in selected_names
        ],
        "full_schema_characters": full_characters,
        "visible_schema_characters": len(
            json.dumps(visible, ensure_ascii=False, default=str)
        ),
    }


def search_tool_schemas(
    schemas: Iterable[dict[str, Any]],
    *,
    query: str = "",
    names: Iterable[str] = (),
    limit: int = 8,
) -> list[dict[str, str]]:
    """Find exact or semantically relevant registered tools deterministically."""

    catalog: list[tuple[str, str]] = []
    for schema in schemas:
        function = schema.get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        description = str(function.get("description") or "").strip().splitlines()[0]
        catalog.append((name, description))
    by_name = {name.casefold(): (name, description) for name, description in catalog}
    requested = [str(name).strip().casefold() for name in names if str(name).strip()]
    results: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for requested_name in requested:
        item = by_name.get(requested_name)
        if item is not None and item[0] not in seen:
            results.append((10_000, item[0], item[1]))
            seen.add(item[0])

    query_terms = _terms(query)
    lowered_query = str(query or "").casefold().strip()
    for name, description in catalog:
        if name in seen:
            continue
        searchable = f"{name} {description}"
        overlap = len(query_terms & _terms(searchable))
        exact_bonus = 200 if lowered_query and lowered_query in searchable.casefold() else 0
        name_bonus = 400 if lowered_query and lowered_query in name.casefold() else 0
        score = overlap * 20 + exact_bonus + name_bonus
        if score > 0 or (not requested and not lowered_query):
            results.append((score, name, description))

    results.sort(key=lambda item: (-item[0], item[1]))
    bounded_limit = min(16, max(1, int(limit)))
    return [
        {"name": name, "description": description}
        for _score, name, description in results[:bounded_limit]
    ]


def build_execution_brief(
    prompt: str,
    profile: AgentProfile,
    schemas: Iterable[dict[str, Any]],
) -> str:
    """Produce a compact, task-specific harness contract for the current run."""

    available = {
        str((schema.get("function") or {}).get("name") or ""):
        str((schema.get("function") or {}).get("description") or "").splitlines()[0]
        for schema in schemas
    }
    shortlist = [name for name in recommended_tool_names(prompt, profile) if name in available]
    tool_lines = "\n".join(f"- {name}: {available[name]}" for name in shortlist[:8])
    discovery_line = (
        "\nIf a needed capability is not visible, call tool_search with the capability or exact tool name; "
        "the host will expose its full schema on the next turn."
        if TOOL_SEARCH_NAME in available
        else ""
    )
    return f"""
CURRENT-TASK TOOL GUIDE
Likely useful tools are listed below only to focus attention. Every registered tool remains available.
{tool_lines or '- Use the smallest relevant set from the available tools.'}
{discovery_line}

Use these suggestions only when they serve the current request, not as a mandatory checklist.
Verification should be proportionate: a check based on the same assumption is not independent proof;
map each completion claim to observed evidence and state any unresolved limitation.
""".strip()


def normalize_tool_envelope(value: Any) -> dict[str, Any]:
    """Give models one unambiguous success bit for heterogeneous tool results."""

    if not isinstance(value, dict):
        return {"ok": True, "result": value}

    def explicit_failure(
        payload: dict[str, Any], *, nested: bool = False
    ) -> tuple[bool, str]:
        """Recognize only explicit failure signals, including one result wrapper.

        Tool adapters use several conventional envelopes.  Treating a non-empty
        ``error`` or an MCP-style nested ``result.ok=false`` as success makes the
        model narrate completion after a real failure.  Limit recursion to the
        canonical ``result`` wrapper so diagnostic lists containing historical
        errors do not poison an otherwise successful status response.
        """

        if payload.get("ok") is False or payload.get("success") is False:
            return True, str(payload.get("error") or "Tool reported failure")
        if payload.get("isError") is True or payload.get("is_error") is True:
            return True, str(payload.get("error") or "Tool reported failure")
        if payload.get("timed_out") is True:
            return True, str(payload.get("error") or "Tool execution timed out")

        status = str(payload.get("status") or "").strip().casefold()
        if status in {
            "failed",
            "failure",
            "error",
            "timed_out",
            "timeout",
            "cancelled",
            "canceled",
            "unavailable",
        }:
            return True, str(
                payload.get("error")
                or payload.get("message")
                or f"Tool reported status {status}"
            )

        exit_code = payload.get("exit_code", payload.get("returncode"))
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            return True, str(payload.get("error") or f"Tool process exited with code {exit_code}")

        status_code = payload.get("status_code")
        if (
            isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and status_code >= 400
        ):
            return True, str(payload.get("error") or f"Tool request failed with HTTP {status_code}")

        error = payload.get("error")
        if error not in (None, "", False, [], {}):
            if isinstance(error, str):
                message = error.strip()
            else:
                message = json.dumps(error, ensure_ascii=False, default=str)
            if message:
                return True, message

        nested_result = payload.get("result")
        if not nested and isinstance(nested_result, dict):
            failed, message = explicit_failure(nested_result, nested=True)
            if failed:
                return True, f"Nested tool result failed: {message}"
        return False, ""

    failed, error = explicit_failure(value)
    if not failed:
        return {"ok": True, "result": value}
    return {"ok": False, "error": error, "result": value}


def serialize_tool_output(value: Any, *, limit: int, prefix: str) -> str:
    """Return valid JSON even when a tool produces more data than the model should ingest."""

    serialized = json.dumps(value, ensure_ascii=False, default=str)
    available = max(256, limit - len(prefix))
    if len(serialized) <= available:
        return prefix + serialized
    def diagnostics(node: Any, path: str = "", depth: int = 0) -> dict[str, Any]:
        if depth > 4:
            return {}
        important = {
            "ok", "error", "errors", "message", "stderr", "stdout", "exit_code",
            "returncode", "timed_out", "status", "status_code", "path", "paths",
            "file", "files", "url", "count", "total",
        }
        found: dict[str, Any] = {}
        if isinstance(node, dict):
            for key, item in node.items():
                item_path = f"{path}.{key}" if path else str(key)
                if str(key).casefold() in important:
                    if isinstance(item, str) and len(item) > 1_200:
                        item = item[:600] + "\n…[clipped]…\n" + item[-600:]
                    found[item_path] = item
                if len(found) < 20 and isinstance(item, (dict, list)):
                    found.update(diagnostics(item, item_path, depth + 1))
                if len(found) >= 20:
                    break
        elif isinstance(node, list):
            for index, item in enumerate(node[:8]):
                found.update(diagnostics(item, f"{path}[{index}]", depth + 1))
                if len(found) >= 20:
                    break
        return found

    salient = diagnostics(value)
    excerpt_budget = max(32, available // 2 - 320)
    while True:
        compact = {
            "ok": value.get("ok") if isinstance(value, dict) else None,
            "truncated": True,
            "original_characters": len(serialized),
            "diagnostics": salient,
            "head": serialized[:excerpt_budget],
            "tail": serialized[-excerpt_budget:],
            "instruction": "Use a focused query or read the referenced file for omitted details.",
        }
        payload = json.dumps(compact, ensure_ascii=False)
        if len(payload) <= available or excerpt_budget == 32:
            break
        excerpt_budget = max(32, excerpt_budget - max(16, (len(payload) - available + 1) // 2))
    if len(payload) > available:
        payload = json.dumps(
            {
                "truncated": True,
                "original_characters": len(serialized),
                "instruction": "Run a focused query for details.",
            },
            ensure_ascii=False,
        )
    return prefix + payload
