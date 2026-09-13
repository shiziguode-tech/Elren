from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import re
import types
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from deepdesk.control_files import ControlFileGuard
from deepdesk.plugins.base import ToolContext, ToolPlugin

logger = logging.getLogger(__name__)


class _GuardedToolPlugin(ToolPlugin):
    """Apply the host control-plane transaction to every registered capability."""

    def __init__(self, plugin: ToolPlugin, guard: ControlFileGuard) -> None:
        self.plugin = plugin
        self.guard = guard
        self.name = plugin.name
        self.description = plugin.description
        self.parameters = plugin.parameters

    def risk(self, arguments: dict[str, Any]):
        return self.plugin.risk(arguments)

    def summarize(self, arguments: dict[str, Any]) -> str:
        return self.plugin.summarize(arguments)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await self.guard.call_plugin(
            self.name,
            arguments,
            context,
            lambda: self.plugin.execute(arguments, context),
        )

    async def cleanup(self, context: ToolContext) -> None:
        await self.guard.call_plugin(
            self.name,
            {"action": "cleanup"},
            context,
            lambda: self.plugin.cleanup(context),
        )


class PluginRegistry:
    def __init__(
        self,
        *,
        control_guard: ControlFileGuard | None = None,
        allow_unsafe_external_plugins: bool = False,
    ) -> None:
        self._plugins: dict[str, ToolPlugin] = {}
        self.discovery_errors: dict[str, str] = {}
        self.control_guard = control_guard
        self.allow_unsafe_external_plugins = bool(allow_unsafe_external_plugins)
        self.quarantined_external_plugins: set[str] = set()

    @staticmethod
    def _validate(plugin: ToolPlugin) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(plugin.name or "")):
            raise ValueError(f"Invalid tool name: {plugin.name!r}")
        if not str(plugin.description or "").strip():
            raise ValueError(f"Tool {plugin.name!r} has no description")
        parameters = plugin.parameters
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(f"Tool {plugin.name!r} must expose an object parameter schema")
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError(f"Tool {plugin.name!r} has an invalid parameter schema")
        missing = [name for name in required if name not in properties]
        if missing:
            raise ValueError(f"Tool {plugin.name!r} requires undefined fields: {missing}")

    def register(self, plugin: ToolPlugin) -> None:
        self._validate(plugin)
        if plugin.name in self._plugins:
            raise ValueError(f"Duplicate tool name: {plugin.name}")
        self._plugins[plugin.name] = (
            _GuardedToolPlugin(plugin, self.control_guard)
            if self.control_guard is not None
            else plugin
        )

    def register_many(self, plugins: Iterable[ToolPlugin]) -> None:
        for plugin in plugins:
            self.register(plugin)

    def get(self, name: str) -> ToolPlugin | None:
        return self._plugins.get(name)

    def schemas(self) -> list[dict]:
        return [plugin.api_schema() for plugin in self._plugins.values()]

    def info(self) -> list[dict]:
        return [
            {"name": plugin.name, "description": plugin.description}
            for plugin in self._plugins.values()
        ]

    def health(self) -> dict:
        """Return non-secret plugin diagnostics for the runtime capability map."""

        return {
            # One quarantined optional extension must not hide working built-in
            # tools. ``ready`` describes whether the runtime has any usable
            # capability; ``degraded`` reports partial discovery failures.
            "ready": bool(self._plugins),
            "degraded": bool(self.discovery_errors),
            "loaded": len(self._plugins),
            "names": sorted(self._plugins),
            "skipped": len(self.discovery_errors),
            "errors": dict(self.discovery_errors),
            "unsafe_external_plugins_enabled": self.allow_unsafe_external_plugins,
            "external_plugins_quarantined": len(self.quarantined_external_plugins),
        }

    async def cleanup(self, context: ToolContext, *, timeout: float = 5.0) -> None:
        """Release every per-task resource without one plugin blocking shutdown.

        Cleanup hooks are independent per plugin.  Running them concurrently
        bounds total cancellation latency to one timeout instead of
        ``plugin_count * timeout`` and ensures a broken extension cannot stop
        the packaged browser or another tool from closing its own resources.
        """

        async def cleanup_one(plugin: ToolPlugin) -> None:
            try:
                await asyncio.wait_for(
                    plugin.cleanup(context), timeout=max(0.1, float(timeout))
                )
            except TimeoutError:
                logger.warning(
                    "Tool cleanup timed out for %s (task %s)",
                    plugin.name,
                    context.task_id,
                )
            except Exception:
                logger.warning(
                    "Tool cleanup failed for %s (task %s)",
                    plugin.name,
                    context.task_id,
                    exc_info=True,
                )

        await asyncio.gather(
            *(cleanup_one(plugin) for plugin in self._plugins.values())
        )

    def discover(self, directory: Path) -> list[str]:
        """Load ToolPlugin subclasses from external .py files."""
        loaded: list[str] = []
        if not directory.exists():
            return loaded
        candidates = [
            path for path in sorted(directory.glob("*.py")) if not path.name.startswith("_")
        ]
        if not self.allow_unsafe_external_plugins:
            for path in candidates:
                self.quarantined_external_plugins.add(path.name)
                self.discovery_errors[path.name] = (
                    "Quarantined: in-process Python plugins execute arbitrary host code. "
                    "A human may opt in only at process startup with "
                    "ELREN_ALLOW_UNSAFE_IN_PROCESS_PLUGINS=1."
                )
            return loaded
        for path in candidates:
            if path.name.startswith("_"):
                continue
            try:
                module_name = f"deepdesk_ext_{path.stem}"

                def import_trusted_source(
                    path: Path = path,
                    module_name: str = module_name,
                ) -> types.ModuleType:
                    source = (
                        self.control_guard.trusted_bytes(path)
                        if self.control_guard is not None
                        else path.read_bytes()
                    )
                    if source is None:
                        raise PermissionError(
                            "External plugin is not present in the trusted startup snapshot"
                        )
                    spec = importlib.util.spec_from_loader(module_name, loader=None)
                    module = types.ModuleType(module_name)
                    module.__file__ = str(path)
                    module.__package__ = ""
                    module.__spec__ = spec
                    exec(  # noqa: S102 - explicit unsafe opt-in, isolated by the host guard
                        compile(source, str(path), "exec"), module.__dict__
                    )
                    return module

                module = (
                    self.control_guard.run_untrusted_import(path.name, import_trusted_source)
                    if self.control_guard is not None
                    else import_trusted_source()
                )
            except Exception as exc:
                self.discovery_errors[path.name] = f"{type(exc).__name__}: {exc}"
                continue
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if (
                    cls is not ToolPlugin
                    and issubclass(cls, ToolPlugin)
                    and cls.__module__ == module.__name__
                ):
                    try:
                        plugin = cls()
                        self.register(plugin)
                    except Exception as exc:
                        self.discovery_errors[f"{path.name}:{cls.__name__}"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue
                    loaded.append(plugin.name)
        return loaded
