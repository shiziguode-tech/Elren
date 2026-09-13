from __future__ import annotations

from pathlib import Path

import pytest

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.plugins.registry import PluginRegistry


class HealthyTool(ToolPlugin):
    name = "healthy_tool"
    description = "A deterministic contract-test tool."
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    def risk(self, arguments: dict) -> Risk:
        return Risk.SAFE

    async def execute(self, arguments: dict, context: ToolContext):
        return {"value": arguments["value"]}


def test_registry_validates_tool_contract_and_reports_health() -> None:
    registry = PluginRegistry()
    registry.register(HealthyTool())

    assert registry.health() == {
        "ready": True,
        "degraded": False,
        "loaded": 1,
        "names": ["healthy_tool"],
        "skipped": 0,
        "errors": {},
        "unsafe_external_plugins_enabled": False,
        "external_plugins_quarantined": 0,
    }


def test_registry_rejects_required_fields_missing_from_schema() -> None:
    class BrokenTool(HealthyTool):
        name = "broken_tool"
        parameters = {"type": "object", "properties": {}, "required": ["missing"]}

    with pytest.raises(ValueError, match="undefined fields"):
        PluginRegistry().register(BrokenTool())


def test_external_plugin_failure_is_quarantined_without_hiding_good_plugins(tmp_path: Path) -> None:
    (tmp_path / "00_broken.py").write_text("raise RuntimeError('broken on import')\n", encoding="utf-8")
    (tmp_path / "10_good.py").write_text(
        """
from deepdesk.models import Risk
from deepdesk.plugins.base import ToolPlugin

class ExternalTool(ToolPlugin):
    name = "external_ok"
    description = "Healthy external tool"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    def risk(self, arguments): return Risk.SAFE
    async def execute(self, arguments, context): return {"ok": True}
""".strip(),
        encoding="utf-8",
    )
    registry = PluginRegistry(allow_unsafe_external_plugins=True)

    assert registry.discover(tmp_path) == ["external_ok"]
    assert registry.get("external_ok") is not None
    assert registry.health()["ready"] is True
    assert registry.health()["degraded"] is True
    assert registry.health()["skipped"] == 1
    assert "00_broken.py" in registry.health()["errors"]
    assert registry.health()["unsafe_external_plugins_enabled"] is True


def test_external_plugins_are_quarantined_by_default(tmp_path: Path) -> None:
    (tmp_path / "unsafe.py").write_text("raise AssertionError('must not execute')\n", "utf-8")
    registry = PluginRegistry()

    assert registry.discover(tmp_path) == []
    assert registry.health()["external_plugins_quarantined"] == 1
    assert "Quarantined" in registry.health()["errors"]["unsafe.py"]


def test_empty_registry_is_not_ready() -> None:
    assert PluginRegistry().health() == {
        "ready": False,
        "degraded": False,
        "loaded": 0,
        "names": [],
        "skipped": 0,
        "errors": {},
        "unsafe_external_plugins_enabled": False,
        "external_plugins_quarantined": 0,
    }
