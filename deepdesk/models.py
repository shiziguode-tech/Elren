from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from deepdesk.speech_transcription import SpeechLanguage


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Risk(str, Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalPolicy(str, Enum):
    CAUTIOUS = "cautious"
    BALANCED = "balanced"
    AUTONOMOUS = "autonomous"


class AgentProfile(str, Enum):
    GENERAL = "general"
    PLANNER = "planner"
    COMPUTER_USE = "computer_use"
    CODER = "coder"
    OFFICE = "office"
    GUARDIAN = "guardian"
    OPENCLAW = "openclaw"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting_approval"
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    type: str
    timestamp: str = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    tool: str
    arguments: dict[str, Any]
    risk: Risk
    summary: str
    created_at: str = Field(default_factory=utc_now)


class DiscussionTeamMember(BaseModel):
    """One durable participant in the optional multi-model discussion team."""

    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    role: Literal["leader", "member"] = "member"
    model: str = Field(default="auto", min_length=1, max_length=240)
    reasoning_effort: Literal[
        "default", "auto", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "default"
    assignment: str = Field(default="", max_length=4_000)
    system_prompt: str = Field(default="", max_length=12_000)


class SubagentRun(BaseModel):
    """Durable, presentation-safe snapshot of one delegated specialist run.

    This model intentionally stores no system prompt, private child transcript,
    hidden reasoning, tool arguments, credentials, or environment data.  The
    main Agent remains the only executor; specialists return bounded reports.
    """

    id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=64)
    dispatch_id: str = Field(min_length=1, max_length=64)
    preset_id: str = Field(min_length=1, max_length=64)
    name_zh: str = Field(min_length=1, max_length=80)
    name_en: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    icon_key: str = Field(default="specialist", min_length=1, max_length=40)
    status: Literal[
        "queued",
        "running",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "interrupted",
    ] = "queued"
    assignment: str = Field(min_length=1, max_length=1_000)
    configured_model: str = Field(default="", max_length=240)
    actual_model: str = Field(default="", max_length=240)
    started_at: str = ""
    finished_at: str = ""
    elapsed_ms: int = Field(default=0, ge=0)
    summary: str = Field(default="", max_length=8_000)
    evidence: list[str] = Field(default_factory=list, max_length=12)
    artifacts: list[str] = Field(default_factory=list, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_approval: bool = False
    error_code: str = Field(default="", max_length=80)


class ConversationTurn(BaseModel):
    """Archived execution, separate from the live run's approvals and cursor."""

    prompt: str
    status: TaskStatus
    result: str | None = None
    error: str | None = None
    events: list[TaskEvent] = Field(default_factory=list)
    attachments: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class AgentTask(BaseModel):
    # Old releases persisted ``private_chat`` and ``privacy_session`` in task
    # JSON.  Pydantic ignores those retired keys while loading an existing
    # workspace; they are never restored into live task state or written back.
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid4().hex)
    prompt: str
    title: str = Field(default="", max_length=60)
    # Sidebar metadata lives separately so an old worker snapshot cannot undo it.
    pinned: bool = Field(default=False, exclude=True)
    project_path: str = Field(default="", max_length=4096)
    title_source: Literal["auto", "user", "inherited"] = "auto"
    context_prompt: str | None = None
    # Flat, durable same-conversation user turns, independent of the bounded
    # tool-event timeline. Never recursively copy old tool transcripts here.
    continuation_instructions: list[str] = Field(default_factory=list, exclude=True)
    # Compatibility for continuations made before flat user history existed.
    # This can contain tool/model text, so it stays historical reference data.
    continuation_legacy_context: str = Field(default="", exclude=True)
    # Runtime-only, read-only reference material selected from earlier public
    # completed chats. It is intentionally excluded from the durable payload so
    # old content is not copied into every subsequent task record.
    cross_conversation_context: str = Field(default="", exclude=True)
    cross_context_task_ids: list[str] = Field(default_factory=list, max_length=50)
    source: Literal["web", "feishu", "telegram", "schedule"] = "web"
    # Durable remote reply route.  Channel settings may change after a task is
    # created or while the app is offline; recovery and redelivery must target
    # the originating conversation, not whichever chat was configured last.
    remote_recipient_id: str = Field(default="", max_length=512)
    remote_recipient_type: Literal["", "open_id", "chat_id"] = ""
    # Host-provided provenance. Models, history, attachments and tool output
    # cannot set this flag; only the live App voice UI or Telegram voice ingress can.
    voice_request: bool = False
    interface_language: Literal["auto", "zh", "en"] = "auto"
    parent_task_id: str | None = None
    status: TaskStatus = TaskStatus.QUEUED
    policy: ApprovalPolicy = ApprovalPolicy.AUTONOMOUS
    agent_profile: AgentProfile = AgentProfile.GENERAL
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    events: list[TaskEvent] = Field(default_factory=list)
    conversation_turns: list[ConversationTurn] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None
    active_key: str = "未连接"
    model_preference: str = Field(default="auto", min_length=1, max_length=240)
    reasoning_effort: Literal["auto", "minimal", "low", "medium", "high", "xhigh", "max"] = "high"
    active_model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=240)
    attachments: list[str] = Field(default_factory=list, max_length=20)
    discussion_team_enabled: bool = False
    discussion_team: list[DiscussionTeamMember] = Field(default_factory=list)
    # Only the safe, bounded run snapshots above are persisted and exposed to
    # the UI. The role catalog and private role instructions remain host-side.
    subagent_runs: list[SubagentRun] = Field(default_factory=list, max_length=24)


class CreateTaskRequest(BaseModel):
    # Keep upgrades tolerant of requests cached by an older WebView build.
    # Retired privacy-mode keys are accepted only as unknown data and ignored.
    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(min_length=1, max_length=20000)
    project_path: str | None = Field(default=None, max_length=4096)
    policy: ApprovalPolicy = ApprovalPolicy.AUTONOMOUS
    agent_profile: AgentProfile = AgentProfile.GENERAL
    model_preference: str = Field(default="auto", min_length=1, max_length=240)
    reasoning_effort: Literal["default", "auto", "minimal", "low", "medium", "high", "xhigh", "max"] = "default"
    attachments: list[str] = Field(default_factory=list, max_length=20)
    interface_language: Literal["auto", "zh", "en"] = "auto"
    voice_request: bool = False


class ProjectPathRequest(BaseModel):
    project_path: str | None = Field(default=None, max_length=4096)


class RunningTaskMessageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(min_length=1, max_length=20000)
    attachments: list[str] = Field(default_factory=list, max_length=20)


class TaskTitlePatch(BaseModel):
    title: str = Field(min_length=1, max_length=60)


class SpeechSynthesisRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1800)
    language: SpeechLanguage = "auto"
    voice_name: str = Field(default="", max_length=240)
    rate: float = Field(default=1.0, ge=0.5, le=2.0)


class ApprovalDecision(BaseModel):
    approved: bool


class HumanActionRequest(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    task_id: str
    summary: str = Field(min_length=1, max_length=300)
    instructions: str = Field(min_length=1, max_length=3000)
    target_window: str = Field(default="", max_length=300)
    target_app: str = Field(default="", max_length=200)
    target_page: str = Field(default="", max_length=500)
    taken_over: bool = False
    outcome: str = Field(default="pending", pattern="^(pending|completed|problem)$")
    issue_description: str = Field(default="", max_length=2000)
    created_at: str = Field(default_factory=utc_now)


class HumanActionDecision(BaseModel):
    completed: bool = True
    issue_description: str = Field(default="", max_length=2000)
    skipped_description: bool = False
