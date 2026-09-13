"""Portable visible conversation only: no tools, system prompts or routing data."""
from __future__ import annotations

import json

from deepdesk.models import AgentTask


def conversation_document(task: AgentTask) -> dict:
    turns = []
    for turn in [*task.conversation_turns, task]:
        messages = [{"role": "user", "content": turn.prompt}]
        last_answer = None
        for event in turn.events:
            if event.type not in {"assistant", "user_message_queued"}:
                continue
            content = event.data.get("display_content", event.data.get("content", ""))
            if not isinstance(content, str) or not content:
                continue
            role = "assistant" if event.type == "assistant" else "user"
            messages.append({"role": role, "content": content})
            if role == "assistant":
                last_answer = content
        if turn.result and turn.result != last_answer:
            messages.append({"role": "assistant", "content": turn.result})
        turns.append({"status": turn.status.value, "created_at": turn.created_at,
                      "updated_at": turn.updated_at, "messages": messages})
    return {"format": "elren-conversation", "version": 1, "id": task.id,
            "title": task.title or "Elren conversation", "turns": turns,
            "note": "Visible messages only. Attachments, tool traces, credentials and hidden reasoning are not included. Not a restorable application backup."}


def export_conversation(task: AgentTask, format: str) -> str:
    document = conversation_document(task)
    if format == "json":
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if format != "markdown":
        raise ValueError("Unsupported conversation export format")
    lines = ["# " + " ".join(document["title"].split()), "", document["note"], ""]
    for index, turn in enumerate(document["turns"], 1):
        lines.extend([f"## Turn {index} · {turn['status']}", ""])
        for message in turn["messages"]:
            lines.extend([f"### {message['role'].title()}", "", message["content"], ""])
    return "\n".join(lines)
