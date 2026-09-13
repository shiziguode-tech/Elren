from __future__ import annotations

from typing import Any

from deepdesk.models import Risk
from deepdesk.plugins.base import ToolContext, ToolPlugin
from deepdesk.scheduler import SchedulePatch, Scheduler, ScheduleRequest


class CronTool(ToolPlugin):
    name = "cron"
    description = (
        "Manage human-readable scheduled tasks: run every day, at a fixed interval, "
        "or once. Set when the task starts, what it should do, how often it repeats, "
        "and optionally when it ends. Times are ISO-8601 values with an explicit offset."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "list", "create", "update", "delete", "run_now"]},
            "id": {"type": "string"},
            "schedule": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short task name."},
                    "prompt": {"type": "string", "description": "What the Agent should complete."},
                    "kind": {"type": "string", "enum": ["daily", "interval", "at"]},
                    "expression": {
                        "type": "string",
                        "description": (
                            "For interval use seconds and for legacy cron use five cron fields. "
                            "For daily/at this may be omitted when start_at is supplied."
                        ),
                    },
                    "start_at": {"type": ["string", "null"], "description": "First run as ISO-8601 with timezone. Null clears the optional interval anchor."},
                    "end_at": {"type": ["string", "null"], "description": "Optional last allowed run as ISO-8601 with timezone. In update, null clears this bound; omission preserves it."},
                    "timezone": {"type": "string", "description": "IANA time zone, for example Asia/Taipei."},
                    "enabled": {"type": "boolean"},
                    "resolve_dispatch": {
                        "type": "string", "enum": ["skip", "retry"],
                        "description": "Update only, separately from all other edits. After explicit user confirmation and checking task history, resolve an uncertain dispatch: skip advances past this occurrence; retry may repeat work already created. Ordinary enable cannot clear this safety pause.",
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler

    def risk(self, arguments: dict[str, Any]) -> Risk:
        action = arguments.get("action")
        if action == "update" and arguments.get("schedule", {}).get("resolve_dispatch"):
            return Risk.HIGH
        if action in {"delete", "run_now"}:
            return Risk.HIGH
        return Risk.MEDIUM if action in {"create", "update"} else Risk.SAFE

    def summarize(self, arguments: dict[str, Any]) -> str:
        return f"定时任务操作：{arguments.get('action', '')}"

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        return await self.scheduler.run_async(self._execute_sync, arguments, context)

    def _execute_sync(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        action = arguments["action"]
        if action == "status":
            return self.scheduler.stats()
        if action == "list":
            return {"schedules": self.scheduler.list()}
        if action == "create":
            return self.scheduler.create(ScheduleRequest.model_validate(arguments.get("schedule", {})))
        schedule_id = arguments.get("id", "")
        if not schedule_id:
            raise ValueError("id is required")
        if action == "update":
            updated = self.scheduler.update(
                schedule_id, SchedulePatch.model_validate(arguments.get("schedule", {}))
            )
            if updated is None:
                raise KeyError(f"Schedule not found: {schedule_id}")
            return updated
        if action == "delete":
            if not self.scheduler.delete(schedule_id):
                raise KeyError(f"Schedule not found: {schedule_id}")
            return {"deleted": True}
        if action == "run_now":
            result = self.scheduler.run_now(schedule_id)
            if result is None:
                raise KeyError(f"Schedule not found: {schedule_id}")
            return result
        raise ValueError(f"Unsupported cron action: {action}")
