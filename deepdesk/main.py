from __future__ import annotations

import asyncio
import copy
import errno
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

from deepdesk.audit import AuditLog
from deepdesk.config import Settings
from deepdesk.control_files import _default_state_path, activate_control_file_guard
from deepdesk.conversation_export import export_conversation
from deepdesk.deepseek import DeepSeekClient
from deepdesk.display_text import (
    add_task_display_overrides,
    normalize_assistant_display_text,
)
from deepdesk.engine import AgentEngine, ApprovalGate, HumanActionGate, TaskManager
from deepdesk.feishu import FeishuBridge
from deepdesk.feishu_pending import PendingFeishuAttachments
from deepdesk.local_models import LocalModelRegistry
from deepdesk.macos_ocr import MacOSOCR
from deepdesk.mcp_runtime import MCPRuntime
from deepdesk.media_generation import FreeMediaGenerator
from deepdesk.mobile_bridge import MobileBridge
from deepdesk.model_selection import ModelSelectionError, resolve_model_selection
from deepdesk.models import (
    AgentProfile,
    ApprovalDecision,
    ApprovalPolicy,
    CreateTaskRequest,
    HumanActionDecision,
    ProjectPathRequest,
    RunningTaskMessageRequest,
    SpeechSynthesisRequest,
    TaskStatus,
    TaskTitlePatch,
)
from deepdesk.openclaw_bridge import OpenClawBridge
from deepdesk.paddle_ocr import PaddleOCR
from deepdesk.platform_support import IS_MACOS, IS_WINDOWS, platform_name
from deepdesk.plugins import PluginRegistry
from deepdesk.plugins.base import run_owned_async, run_owned_thread, wait_owned_result
from deepdesk.plugins.builtin import (
    BackgroundBrowserTool,
    ClipboardTool,
    ComputerTool,
    ComputerUseAgentTool,
    CronTool,
    DocumentTool,
    FeishuTool,
    FileSystemTool,
    HumanActionTool,
    JianpuOMRTool,
    JianpuToStaffTool,
    LiveComputerUseTool,
    MacOSUITool,
    MCPTool,
    MediaGenerationTool,
    MemoryTool,
    MobileDeviceTool,
    OpenClawBridgeTool,
    ProcessManagerTool,
    ProviderWebSearchTool,
    RemoteSettingsTool,
    SandboxTool,
    ShellTool,
    SkillsTool,
    TelegramTool,
    VisionTool,
    WebTool,
    WindowsUITool,
)
from deepdesk.posix_sandbox import PosixResourceSandbox
from deepdesk.provider_secrets import ProviderSecrets, ProviderSecretsStore
from deepdesk.runtime_settings import RuntimeSettings, RuntimeSettingsPatch, RuntimeSettingsStore
from deepdesk.scheduler import SchedulePatch, Scheduler, ScheduleRequest
from deepdesk.settings_transaction import (
    SettingsCandidateError,
    SettingsRecoveryError,
    SettingsTransaction,
    copy_settings_state,
)
from deepdesk.speech_synthesis import SpeechSynthesizer
from deepdesk.speech_transcription import SpeechTranscriber, system_speech_language
from deepdesk.subprocess_env import credential_safe_environment
from deepdesk.task_store import TaskStore
from deepdesk.task_titles import clean_generated_title
from deepdesk.telegram import TelegramBridge
from deepdesk.tool_credentials import SKILL_CREDENTIAL_ENV, ToolCredentialResolver
from deepdesk.tool_runtime import (
    ToolRuntimeManifestError,
    discover_tool_runtime,
    packaged_workspace_root,
)
from deepdesk.upload_storage import save_upload
from deepdesk.vision_runtime import VisionRuntime
from deepdesk.web_security import LoopbackWebSecurityMiddleware
from deepdesk.window_focus import focus_window
from deepdesk.windows_ocr import WindowsOCR

DISCUSSION_TEAM_MODEL_SELECTOR = "discussion-team"


def log_telegram_delivery_failure(action: str, exc: BaseException) -> None:
    """Record Telegram delivery failures without serializing secret-bearing URLs."""

    logger.debug("%s (%s)", action, type(exc).__name__)


async def _monitor_feishu_connection(
    bridge: FeishuBridge,
    *,
    check_interval_seconds: float = 10.0,
    unhealthy_threshold: int = 3,
    failure_backoff_seconds: float = 5.0,
    max_failure_backoff_seconds: float = 60.0,
) -> None:
    """Keep the Feishu listener supervised across transient local failures.

    Listener exceptions can include credential-bearing HTTP details. The
    supervisor records only the operation and exception type, then retries
    with a bounded backoff instead of terminating its task.
    """

    interval = max(0.0, float(check_interval_seconds))
    threshold = max(1, int(unhealthy_threshold))
    initial_backoff = max(0.001, float(failure_backoff_seconds))
    maximum_backoff = max(initial_backoff, float(max_failure_backoff_seconds))
    unhealthy_checks = 0
    backoff = initial_backoff
    while True:
        await asyncio.sleep(interval)
        try:
            current = await bridge.status(probe=False)
            if not current.get("configured") or current.get("ready"):
                unhealthy_checks = 0
                backoff = initial_backoff
                continue
            unhealthy_checks += 1
            if unhealthy_checks < threshold:
                continue
            unhealthy_checks = 0
            await bridge.restart_long_connection()
            backoff = initial_backoff
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Feishu connection monitor will retry (%s)",
                type(exc).__name__,
            )
            await asyncio.sleep(backoff)
            backoff = min(maximum_backoff, backoff * 2)


async def _cancel_and_observe_background_task(
    task: asyncio.Task[Any] | None,
    *,
    component: str,
) -> dict[str, str] | None:
    """Cancel one owned task and return a sanitized pre-shutdown failure."""

    if task is None:
        return None
    if not task.done():
        task.cancel()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    if isinstance(result, asyncio.CancelledError) or not isinstance(result, BaseException):
        return None
    failure = {"component": component, "error_type": type(result).__name__}
    logger.error(
        "Background task failed before shutdown: %s (%s)",
        component,
        failure["error_type"],
    )
    return failure


def authorize_or_bind_telegram_chat(
    bridge: TelegramBridge,
    secrets_store: ProviderSecretsStore,
    incoming_chat_id: str,
    *,
    chat_type: str,
) -> bool:
    """Authorize the bound Telegram chat or durably claim the first private chat."""

    incoming = str(incoming_chat_id or "").strip()
    if not incoming:
        return False
    bound = bridge.default_chat_id.strip()
    if bound:
        return incoming == bound
    # Automatic first-message pairing is limited to a direct chat. A bot can be
    # added to an arbitrary group without the owner's consent; allowing that
    # group to win first-message pairing would expose an autonomous local agent.
    if str(chat_type or "").strip().casefold() != "private":
        return False
    # Persist first, then update the in-memory route. Both calls are synchronous,
    # so this claim is indivisible within Telegram's single event-loop callback.
    secrets_store.update(telegram_chat_id=incoming)
    bridge.remember_default_chat_id(incoming)
    return True


def resolve_task_model_preference(
    selector: str,
    runtime: RuntimeSettings,
) -> tuple[bool, str]:
    """Resolve the model-picker's team pseudo-model to the leader's base model."""

    normalized = str(selector or "auto").strip() or "auto"
    if normalized != DISCUSSION_TEAM_MODEL_SELECTOR:
        return False, normalized
    if not runtime.discussion_team_enabled:
        raise ValueError("讨论团尚未在设置中完成配置并保存")
    leader = next(
        (member for member in runtime.discussion_team if member.role == "leader"),
        None,
    )
    if leader is None or len(runtime.discussion_team) < 2:
        raise ValueError("讨论团配置无效：至少需要两名成员且必须指定一名组长")
    return True, leader.model or "auto"


def remote_channel_task_snapshot(
    runtime: RuntimeSettings,
    *,
    automatic_model: str,
) -> dict[str, Any]:
    """Freeze the saved default model for one Feishu or Telegram task.

    A saved discussion-team roster only makes the team available in the web
    model picker.  It must not silently turn every remote-channel message into
    a team task.  Remote channels have no per-message model picker, so they use
    the explicitly saved default model and never infer team selection merely
    from the existence of a roster.
    """

    selected_model = str(runtime.model or "auto").strip() or "auto"
    if selected_model == DISCUSSION_TEAM_MODEL_SELECTOR:
        # The Settings default-model selector never offers this pseudo-model.
        # Treat stale or hand-edited legacy state conservatively instead of
        # enabling a team without a current, explicit task selection.
        selected_model = "auto"
    return {
        "model_preference": selected_model,
        "active_model": (
            selected_model if selected_model != "auto" else automatic_model
        ),
        "reasoning_effort": runtime.reasoning_effort,
        "discussion_team_enabled": False,
        "discussion_team": [],
    }
from deepdesk.windows_sandbox import WindowsJobSandbox


def portable_runtime_status(project_root: Path) -> dict[str, object]:
    """Report the package-local runtime without exposing host-specific paths."""

    project_root = packaged_workspace_root(project_root)
    manifest_path = project_root / "work" / "runtime-bundle.json"
    if not manifest_path.is_file():
        return {"ready": False, "mode": "system-fallback", "components": {}}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("bundle") != "elren-portable-runtime":
            raise ValueError("unsupported portable runtime manifest")
        components = manifest.get("components") or {}
        if not isinstance(components, dict):
            raise ValueError("portable runtime components must be an object")
        compact: dict[str, dict[str, object]] = {}
        ready = True
        for name in ("python", "node", "openclaw"):
            item = components.get(name) or {}
            if not isinstance(item, dict):
                raise ValueError("portable runtime component must be an object")
            relative = str(item.get("executable") or item.get("entry") or "")
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("portable runtime path must stay inside the package")
            (project_root / relative).resolve().relative_to(project_root.resolve())
            present = bool(relative and (project_root / Path(relative)).is_file())
            ready = ready and present
            compact[name] = {"version": str(item.get("version") or ""), "present": present}
        try:
            tool_runtime = discover_tool_runtime(project_root)
            tool_runtime_status = (
                tool_runtime.status()
                if tool_runtime
                else {"ready": False, "tool_count": 0, "available_tool_count": 0}
            )
        except ToolRuntimeManifestError:
            tool_runtime_status = {
                "ready": False,
                "mode": "invalid-manifest",
                "tool_count": 0,
                "available_tool_count": 0,
            }
        compact["tool_runtime"] = tool_runtime_status
        ready = ready and bool(tool_runtime_status.get("ready"))
        return {
            "ready": ready,
            "mode": str(manifest.get("isolation") or "package-first-with-system-fallback"),
            "components": compact,
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"ready": False, "mode": "invalid-manifest", "components": {}}


def _referenced_output_paths(
    task: Any,
    workspace: Path,
    extensions: tuple[str, ...],
    *,
    max_bytes: int,
    max_results: int = 10,
) -> list[Path]:
    """Resolve final-answer artifacts without allowing arbitrary file exfiltration.

    Remote channels may return files created for a task, but model-authored text
    must never be able to turn an unrelated absolute path into an attachment.
    Only real files beneath the workspace ``outputs`` directory are eligible.
    Resolving both the root and candidate also rejects symlinks that escape it.
    """

    workspace = workspace.expanduser().resolve()
    output_root = (workspace / "outputs").resolve()
    suffixes = {f".{value.lstrip('.').casefold()}" for value in extensions}
    text = str(getattr(task, "result", "") or "")
    # This is the existing literal-path contract, not a URI attachment parser.
    # Never reinterpret part of a URL as a local path, or decode percent escapes.
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\"`]+", "", text)
    starts = re.compile(r"(?i)(?:[a-z]:[\\/]|/|outputs[\\/])")
    references: list[str] = []
    consumed = 0
    for start in starts.finditer(text):
        if start.start() < consumed:
            continue
        closing: list[str] = []
        preceding = text[start.start() - 1] if start.start() > 0 else ""
        single_quoted = preceding == "'"
        outer_closer = {"(": ")", "[": "]", "{": "}"}.get(preceding)
        end = start.end()
        for end in range(start.end(), len(text)):
            char = text[end]
            if char in '\r\n<>|*?':
                break
            if char in '\"`' and preceding == char and (
                end + 1 == len(text) or text[end + 1].isspace()
                or text[end + 1] in '\"<>`)]}'
            ):
                break
            if char in "([{":
                closing.append({"(": ")", "[": "]", "{": "}"}[char])
            elif char in ")]}":
                if not closing or closing[-1] != char:
                    if char == outer_closer and (
                        end + 1 == len(text) or text[end + 1].isspace()
                        or text[end + 1] in '\"<>`)]}'
                    ):
                        break
                else:
                    closing.pop()
        else:
            end = len(text)
        consumed = max(end, start.end())
        # Keep the complete reference, including earlier extensions. A missing
        # versioned file must never fall back to an existing prefix/old result.
        reference = text[start.start():end].rstrip(" \t")
        if single_quoted and reference.endswith("'"):
            reference = reference[:-1]
        references.append(reference)

    attachment_paths: set[str] = set()
    for raw_attachment in getattr(task, "attachments", []) or []:
        attachment = Path(str(raw_attachment))
        if not attachment.is_absolute():
            attachment = workspace / attachment
        try:
            attachment_paths.add(str(attachment.resolve()).casefold())
        except OSError:
            continue

    found: list[Path] = []
    seen: set[str] = set()
    for raw in references:
        candidate = Path(raw)
        if candidate.suffix.casefold() not in suffixes:
            continue
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            candidate = candidate.resolve()
            candidate.relative_to(output_root)
            key = str(candidate).casefold()
            if (
                key in attachment_paths
                or key in seen
                or not candidate.is_file()
                or candidate.stat().st_size > max_bytes
            ):
                continue
        except (OSError, ValueError):
            continue
        seen.add(key)
        found.append(candidate)
        if len(found) >= max_results:
            break
    return found


def create_app(
    settings: Settings | None = None,
    *,
    persistent_control_baseline: bool = False,
) -> FastAPI:
    settings = settings or Settings()
    plugin_dir = settings.workspace / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    # Recover before either settings reader or the control watchdog can publish
    # half of an interrupted settings transaction.
    recovery_targets = {
        "credentials": settings.data_dir / "provider-secrets.vault",
        "preferences": settings.data_dir / "runtime-settings.json",
        "control": _default_state_path(settings.workspace),
    }
    SettingsTransaction(settings.data_dir, recovery_targets).recover()
    control_guard = activate_control_file_guard(
        settings.workspace,
        persistent=persistent_control_baseline,
    )
    provider_secrets = ProviderSecretsStore(
        settings.data_dir / "provider-secrets.vault",
        ProviderSecrets(
            deepseek_primary=settings.deepseek_api_key,
            deepseek_backup=settings.deepseek_backup_api_key,
            gemini=settings.deepdesk_vision_api_key,
            pollinations=settings.deepdesk_pollinations_api_key,
            huggingface=settings.deepdesk_huggingface_token,
            aicodemirror=settings.deepdesk_aicodemirror_api_key,
            aicodemirror_fable=settings.deepdesk_aicodemirror_fable_api_key,
            feishu_app_id=settings.deepdesk_feishu_app_id,
            feishu_app_secret=settings.deepdesk_feishu_app_secret,
            feishu_open_id=settings.deepdesk_feishu_open_id,
            telegram_bot_token=settings.deepdesk_telegram_bot_token,
            telegram_chat_id=settings.deepdesk_telegram_chat_id,
            github_token=settings.deepdesk_github_token,
            google_places_api_key=settings.deepdesk_google_places_api_key,
            trello_api_key=settings.deepdesk_trello_api_key,
            trello_token=settings.deepdesk_trello_token,
            elevenlabs_api_key=settings.deepdesk_elevenlabs_api_key,
            notion_token=settings.deepdesk_notion_token,
            spotify_client_id=settings.deepdesk_spotify_client_id,
            spotify_client_secret=settings.deepdesk_spotify_client_secret,
            op_service_account_token=settings.deepdesk_op_service_account_token,
            giphy_api_key=settings.deepdesk_giphy_api_key,
            tenor_api_key=settings.deepdesk_tenor_api_key,
            apify_api_token=settings.deepdesk_apify_api_token,
            firecrawl_api_key=settings.deepdesk_firecrawl_api_key,
            eightctl_email=settings.deepdesk_eightctl_email,
            eightctl_password=settings.deepdesk_eightctl_password,
            deliveroo_bearer_token=settings.deepdesk_deliveroo_bearer_token,
            deliveroo_cookie=settings.deepdesk_deliveroo_cookie,
            things_auth_token=settings.deepdesk_things_auth_token,
            sag_api_key=settings.deepdesk_sag_api_key,
        ),
        legacy_path=settings.data_dir / "provider-keys.json",
        legacy_env_path=settings.workspace / ".env",
    )
    secrets = provider_secrets.value

    def configured_model_key(provider: str) -> str:
        for entry in provider_secrets.value.model_providers:
            # Compatible third-party keys are not official provider/tool keys.
            if entry.get("source") == "custom" or "base_url" in entry:
                continue
            if str(entry.get("provider") or "").casefold() == provider.casefold():
                key = str(entry.get("api_key") or "")
                if key:
                    return key
        if provider == "google":
            return provider_secrets.value.gemini
        return ""

    def configured_secret_values() -> list[str]:
        """Return live credential values for in-memory persistence redactors."""

        current = provider_secrets.value
        return [
            current.deepseek_primary,
            current.deepseek_backup,
            current.openclaw_deepseek_api_key,
            current.gemini,
            current.pollinations,
            current.huggingface,
            current.aicodemirror,
            current.aicodemirror_fable,
            current.feishu_app_secret,
            current.feishu_verification_token,
            current.feishu_encrypt_key,
            current.telegram_bot_token,
            current.github_token,
            current.google_places_api_key,
            current.trello_api_key,
            current.trello_token,
            current.elevenlabs_api_key,
            current.notion_token,
            current.spotify_client_id,
            current.spotify_client_secret,
            current.op_service_account_token,
            current.giphy_api_key,
            current.tenor_api_key,
            current.apify_api_token,
            current.firecrawl_api_key,
            current.eightctl_email,
            current.eightctl_password,
            current.deliveroo_bearer_token,
            current.deliveroo_cookie,
            current.things_auth_token,
            current.sag_api_key,
            *[
                str(item.get("api_key") or "")
                for item in current.model_providers
            ],
        ]

    tool_credentials = ToolCredentialResolver(
        lambda: {
            "GH_TOKEN": provider_secrets.value.github_token,
            "GOOGLE_PLACES_API_KEY": provider_secrets.value.google_places_api_key,
            "TRELLO_API_KEY": provider_secrets.value.trello_api_key,
            "TRELLO_TOKEN": provider_secrets.value.trello_token,
            "ELEVENLABS_API_KEY": provider_secrets.value.elevenlabs_api_key,
            "NOTION_API_TOKEN": provider_secrets.value.notion_token,
            "SPOTIFY_CLIENT_ID": provider_secrets.value.spotify_client_id,
            "SPOTIFY_CLIENT_SECRET": provider_secrets.value.spotify_client_secret,
            "OPENAI_API_KEY": configured_model_key("openai"),
            "ANTHROPIC_API_KEY": configured_model_key("anthropic"),
            "GEMINI_API_KEY": configured_model_key("google"),
            "GOOGLE_API_KEY": configured_model_key("google"),
            "GOOGLE_GENERATIVE_AI_API_KEY": configured_model_key("google"),
            "XAI_API_KEY": configured_model_key("xai"),
            "OP_SERVICE_ACCOUNT_TOKEN": provider_secrets.value.op_service_account_token,
            "GIPHY_API_KEY": provider_secrets.value.giphy_api_key,
            "TENOR_API_KEY": provider_secrets.value.tenor_api_key,
            "APIFY_API_TOKEN": provider_secrets.value.apify_api_token,
            "FIRECRAWL_API_KEY": provider_secrets.value.firecrawl_api_key,
            "EIGHTCTL_EMAIL": provider_secrets.value.eightctl_email,
            "EIGHTCTL_PASSWORD": provider_secrets.value.eightctl_password,
            "DELIVEROO_BEARER_TOKEN": provider_secrets.value.deliveroo_bearer_token,
            "DELIVEROO_COOKIE": provider_secrets.value.deliveroo_cookie,
            "THINGS_AUTH_TOKEN": provider_secrets.value.things_auth_token,
            "SAG_API_KEY": provider_secrets.value.sag_api_key,
        }
    )
    runtime_store = RuntimeSettingsStore(
        settings.data_dir / "runtime-settings.json",
        RuntimeSettings(
            model=settings.deepseek_model,
            max_output_tokens=settings.deepdesk_max_output_tokens or None,
            request_timeout=settings.deepdesk_request_timeout,
        ),
    )
    feishu_bridge = FeishuBridge(
        enabled=settings.deepdesk_feishu_enabled,
        base_url=settings.deepdesk_feishu_base_url,
        app_id=secrets.feishu_app_id or settings.deepdesk_feishu_app_id,
        app_secret=secrets.feishu_app_secret or settings.deepdesk_feishu_app_secret,
        # Legacy callback values remain loadable from older local stores, but
        # new users only configure App ID/Secret and use the long connection.
        verification_token=secrets.feishu_verification_token,
        encrypt_key=secrets.feishu_encrypt_key,
        default_receive_id=secrets.feishu_open_id or settings.deepdesk_feishu_open_id,
        workspace=settings.workspace,
    )
    telegram_bridge = TelegramBridge(
        bot_token=secrets.telegram_bot_token or settings.deepdesk_telegram_bot_token,
        default_chat_id=secrets.telegram_chat_id or settings.deepdesk_telegram_chat_id,
    )
    vision_runtime = VisionRuntime(
        base_url=settings.deepdesk_vision_base_url,
        api_key=secrets.gemini,
        model=settings.deepdesk_vision_model,
        auto_discover=settings.deepdesk_vision_auto_discover,
        start_command=settings.deepdesk_vision_start_command,
        start_timeout=settings.deepdesk_vision_start_timeout,
        workspace=settings.workspace,
        relay_api_key=secrets.aicodemirror,
        deepseek_keys=lambda: (
            provider_secrets.value.deepseek_primary,
            provider_secrets.value.deepseek_backup,
        ),
    )
    windows_ocr = WindowsOCR() if IS_WINDOWS else MacOSOCR()
    paddle_ocr = PaddleOCR()
    # Dedicated live-control route: does not mutate ordinary OCR/chat priority.
    if IS_WINDOWS:
        from deepdesk.live_control_vision import LiveVisualJudge
    live_computer_use_tool = (
        LiveComputerUseTool(settings.screenshot_dir, windows_ocr,
            visual_judge=LiveVisualJudge(vision_runtime.semantic_fallback_endpoints))
        if IS_WINDOWS and isinstance(windows_ocr, WindowsOCR)
        else None
    )
    web_tool = WebTool()
    background_browser_tool = BackgroundBrowserTool(
        workspace=settings.workspace,
        screenshot_dir=settings.screenshot_dir,
    )
    openclaw_bridge = OpenClawBridge(
        enabled=settings.deepdesk_openclaw_enabled,
        cli=settings.deepdesk_openclaw_cli,
        gateway_url=settings.deepdesk_openclaw_gateway_url,
        gateway_token=settings.deepdesk_openclaw_gateway_token,
        auto_start=settings.deepdesk_openclaw_auto_start,
        workspace=settings.workspace,
        provider_env={
            "DEEPSEEK_API_KEY": (
                secrets.openclaw_deepseek_api_key or secrets.deepseek_primary
            )
        },
        provider_secrets=provider_secrets,
    )
    openclaw_bridge.set_skill_credentials(
        {skill: tool_credentials.environment_for(skill) for skill in SKILL_CREDENTIAL_ENV}
    )
    mcp_runtime = MCPRuntime(settings.workspace)
    media_generator = FreeMediaGenerator(
        secrets.pollinations,
        settings.workspace / "outputs",
        huggingface_token=secrets.huggingface,
    )
    sandbox = (
        WindowsJobSandbox(settings.workspace)
        if IS_WINDOWS
        else PosixResourceSandbox(settings.workspace)
    )
    speech_transcriber = SpeechTranscriber(lambda: provider_secrets.value.huggingface)
    speech_synthesizer = SpeechSynthesizer(settings.data_dir / "speech-cache")
    mobile_bridge = MobileBridge(
        settings.data_dir,
        settings.screenshot_dir,
        settings.deepdesk_mobile_port,
    )
    speech_stream_sessions: dict[str, tuple[float, SpeechSynthesisRequest]] = {}
    settings_update_ref: dict[str, Any] = {}
    memory_tool = MemoryTool(
        settings.data_dir / "deepdesk.db",
        secret_values=configured_secret_values,
    )
    registry = PluginRegistry(
        control_guard=control_guard,
        allow_unsafe_external_plugins=(
            os.environ.get("ELREN_ALLOW_UNSAFE_IN_PROCESS_PLUGINS", "") == "1"
        ),
    )
    phone_vision = VisionTool(
        vision_runtime, settings.screenshot_dir, windows_ocr=windows_ocr,
        paddle_ocr=paddle_ocr, active_model=lambda: client.effective_model,
    )
    builtin_tools = [
            FileSystemTool(),
            DocumentTool(settings.screenshot_dir),
            HumanActionTool(),
            JianpuOMRTool(settings.screenshot_dir),
            JianpuToStaffTool(settings.screenshot_dir),
            memory_tool,
            MobileDeviceTool(mobile_bridge, settings.screenshot_dir, vision=phone_vision),
            MediaGenerationTool(media_generator),
            MCPTool(mcp_runtime),
            SkillsTool(tool_credentials.status_for),
            ShellTool(sandbox, tool_credentials.environment_for),
            ProcessManagerTool(),
            ClipboardTool(),
            background_browser_tool,
            ComputerTool(settings.screenshot_dir),
            phone_vision,
            web_tool,
            OpenClawBridgeTool(openclaw_bridge),
            RemoteSettingsTool(settings_update_ref, settings.workspace / "outputs"),
            FeishuTool(feishu_bridge),
            TelegramTool(telegram_bridge),
            SandboxTool(sandbox),
        ]
    if IS_WINDOWS:
        builtin_tools.extend(
            [
                WindowsUITool(),
                ComputerUseAgentTool(settings.screenshot_dir),
                live_computer_use_tool,
            ]
        )
    elif IS_MACOS:
        builtin_tools.append(MacOSUITool())
    registry.register_many(builtin_tools)
    registry.discover(plugin_dir)

    approvals = ApprovalGate()
    human_actions = HumanActionGate()
    client = DeepSeekClient(
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        primary_key=secrets.deepseek_primary,
        backup_key=secrets.deepseek_backup,
        timeout=settings.deepdesk_request_timeout,
        max_output_tokens=settings.deepdesk_max_output_tokens or None,
    )
    client.set_provider_models(
        secrets.model_providers,
        {
            "anthropic": secrets.aicodemirror,
            "openai": secrets.aicodemirror,
            "google": secrets.aicodemirror,
        },
        secrets.aicodemirror_fable,
    )
    saved_default_model = runtime_store.value.model
    if saved_default_model.startswith("local-"):
        # Local discovery is intentionally asynchronous so an offline runtime
        # cannot delay the desktop UI. Preserve the user's preference until
        # the first loopback probe can validate and activate it.
        client.default_model_preference = saved_default_model
    else:
        client.set_default_model_preference(saved_default_model)
    if (
        not saved_default_model.startswith("local-")
        and saved_default_model != client.default_model_preference
    ):
        with control_guard.runtime_settings_transaction():
            runtime_store.update(
                RuntimeSettingsPatch(model=client.default_model_preference)
            )
    local_model_registry = LocalModelRegistry()
    registry.register(
        ProviderWebSearchTool(
            client,
            web_tool=web_tool,
            background_browser_tool=background_browser_tool,
        )
    )
    audit = AuditLog(settings.data_dir / "audit.jsonl")
    task_store = TaskStore(
        settings.data_dir / "deepdesk.db",
        route_protector=provider_secrets.protector,
    )
    manager_ref: dict[str, TaskManager] = {}
    auxiliary_tasks: set[asyncio.Task[Any]] = set()
    settings_update_lock = asyncio.Lock()

    def spawn_auxiliary(coroutine, *, name: str) -> asyncio.Task[Any]:
        """Keep fire-and-forget work alive and always observe its exception."""
        task = asyncio.create_task(coroutine, name=name)
        auxiliary_tasks.add(task)

        def finished(completed: asyncio.Task[Any]) -> None:
            auxiliary_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.error(
                    "Background task %s failed",
                    completed.get_name(),
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)
        return task

    async def refresh_local_models() -> dict[str, Any]:
        discovered = await local_model_registry.discover()
        client.set_local_models(discovered)
        vision_runtime.set_local_model_capabilities(discovered)
        client.set_default_model_preference(runtime_store.value.model)
        if any(item.get("supports_vision") is True for item in discovered):
            await vision_runtime.discover()
        return local_model_registry.status()

    async def monitor_local_models() -> None:
        while True:
            try:
                await refresh_local_models()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Local model discovery failed")
            await asyncio.sleep(30)

    def emit(task, event_type, data):
        return manager_ref["manager"].emit_async(task, event_type, data)

    async def surface_human_action(task_id: str) -> None:
        url = f"http://{settings.deepdesk_host}:{settings.deepdesk_port}/?task={task_id}&handoff=1"
        launcher = settings.workspace / "Elren.exe"
        # A packaged desktop session is activated through the launcher's
        # single-instance channel, including the exact task route.  Keep the
        # browser fallback only for source/development runs.
        desktop_shell = os.getenv(
            "ELREN_DESKTOP_SHELL",
            os.getenv("MILO_DESKTOP_SHELL", os.getenv("DEEPDESK_DESKTOP_SHELL", "")),
        )
        if desktop_shell == "1" and launcher.is_file():
            await asyncio.to_thread(
                subprocess.Popen,
                [str(launcher), f"--task={task_id}"],
                cwd=str(settings.workspace),
                env=credential_safe_environment(),
            )
        else:
            await asyncio.to_thread(webbrowser.open, url)
        await asyncio.sleep(0.2)
        await focus_window("Elren")

    def on_human_action(request) -> None:
        spawn_auxiliary(
            surface_human_action(request.task_id),
            name=f"surface-human-action-{request.task_id}",
        )

    def on_approval(request) -> None:
        spawn_auxiliary(
            surface_human_action(request.task_id),
            name=f"surface-approval-{request.task_id}",
        )

    engine = AgentEngine(
        client=client,
        registry=registry,
        approvals=approvals,
        human_actions=human_actions,
        audit=audit,
        workspace=str(settings.workspace),
        max_steps=runtime_store.value.max_steps,
        emit=emit,
        on_human_action=on_human_action,
        on_approval=on_approval,
        secret_values=configured_secret_values,
        custom_system_prompt_suffix=(
            lambda: control_guard.trusted_runtime_system_prompt_suffix
        ),
    )

    def cross_conversation_context(
        prompt: str,
        source: str = "",
        remote_recipient_id: str = "",
        remote_recipient_type: str = "",
    ) -> tuple[str, list[str]]:
        if not runtime_store.value.cross_conversation_context:
            return "", []
        return task_store.build_cross_conversation_context(
            prompt,
            source=source,
            remote_recipient_id=remote_recipient_id,
            remote_recipient_type=remote_recipient_type,
        )

    manager = TaskManager(
        engine,
        approvals,
        task_store,
        cross_context_provider=cross_conversation_context,
    )
    # Repair malformed model-generated titles, including one-character output.
    # User-renamed chats are never modified.
    for restored_task in manager.tasks.values():
        if restored_task.title_source != "auto":
            continue
        try:
            clean_generated_title(restored_task.prompt, restored_task.title)
        except ValueError:
            restored_task.title = TaskManager._initial_title(restored_task.prompt)
            task_store.save(restored_task)
    manager_ref["manager"] = manager

    def discussion_team_snapshot(*, enabled: bool | None = None) -> dict[str, Any]:
        """Capture one task's team so later Settings edits cannot rewrite it."""

        return {
            "discussion_team_enabled": (
                runtime_store.value.discussion_team_enabled
                if enabled is None
                else enabled
            ),
            "discussion_team": [
                member.model_copy(deep=True)
                for member in runtime_store.value.discussion_team
            ],
        }

    async def generate_task_title(task) -> None:
        if task.parent_task_id or task.title_source != "auto" or task.conversation_turns:
            return
        selected_model = (
            task.model_preference if task.model_preference != "auto" else ""
        )
        # The engine is scheduled before this presentation-only job. For
        # Automatic routing, wait for its concrete selection instead of letting
        # a hard-coded model misidentify itself in the conversation title.
        for _ in range(600):
            current = await manager.get_async(task.id)
            if current is not task or current.title_source != "auto":
                return
            selected_event = next(
                (
                    event
                    for event in reversed(current.events)
                    if event.type == "model_selected" and event.data.get("model")
                ),
                None,
            )
            if selected_event:
                selected_model = str(selected_event.data["model"])
                break
            background = manager.background.get(task.id)
            if background is not None and background.done():
                break
            await asyncio.sleep(0.05)
        if not selected_model:
            # Preserve the compact host-generated title rather than naming the
            # chat with a model that did not actually execute it.
            return
        try:
            title = await client.generate_task_title(
                task.prompt, model_selector=selected_model
            )
        except Exception:
            # The compact host-generated title remains available when the
            # selected provider is offline, rate-limited, or not configured.
            return
        current = await manager.get_async(task.id)
        if current is not task or current.title_source != "auto":
            return
        try:
            await manager.rename_async(current.id, title, current.title_source, expected_task=task)
        except (OSError, sqlite3.Error):
            # Presentation-only enrichment must not publish a phantom title or
            # interfere with the actual answer when storage is unavailable.
            logger.warning("Automatic task title could not be saved")

    def schedule_task_title(task) -> None:
        spawn_auxiliary(generate_task_title(task), name=f"task-title-{task.id}")

    manager.on_task_created = schedule_task_title
    client.timeout = runtime_store.value.request_timeout
    client.max_output_tokens = runtime_store.value.max_output_tokens
    async def dispatch_scheduled_task(prompt, _policy, profile):
        task = await manager.create_async(
            prompt,
            ApprovalPolicy.AUTONOMOUS,
            AgentProfile.GENERAL,
            source="schedule",
            reasoning_effort=runtime_store.value.reasoning_effort,
            **discussion_team_snapshot(),
        )
        return task.id

    scheduler = Scheduler(
        settings.data_dir / "deepdesk.db",
        dispatch_scheduled_task,
        secret_values=configured_secret_values,
    )
    registry.register(CronTool(scheduler))

    feishu_listener_url = (
        f"http://127.0.0.1:{settings.deepdesk_port}/api/feishu/long-connection"
    )
    feishu_monitor_task: asyncio.Task | None = None
    channel_bootstrap_task: asyncio.Task | None = None
    pending_feishu_attachments = PendingFeishuAttachments(delay_seconds=15)
    pending_telegram_attachments = PendingFeishuAttachments(delay_seconds=15)

    async def initialize_message_channels() -> None:
        nonlocal feishu_monitor_task
        # External channels must never hold the local HTTP interface hostage on
        # a slow/offline PC. Their own status/retry paths can recover later while
        # the core UI remains available.
        try:
            await feishu_bridge.start_long_connection(feishu_listener_url)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background startup failed for Feishu listener")
        try:
            await recover_interrupted_feishu_tasks()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background startup failed for Feishu task recovery")
        try:
            await telegram_bridge.start_polling(accept_telegram_update)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background startup failed for Telegram polling")
        try:
            await recover_interrupted_telegram_tasks()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background startup failed for Telegram task recovery")
        feishu_monitor_task = asyncio.create_task(
            _monitor_feishu_connection(feishu_bridge),
            name="feishu-connection-monitor",
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal channel_bootstrap_task, feishu_monitor_task
        await scheduler.start()
        if vision_runtime.configured:
            spawn_auxiliary(vision_runtime.discover(), name="vision-initial-probe")
        spawn_auxiliary(monitor_local_models(), name="local-model-monitor")
        if mobile_bridge.has_devices:
            try:
                await mobile_bridge.start()
            except Exception:
                logger.exception("Background startup failed for Android companion bridge")
        channel_bootstrap_task = asyncio.create_task(initialize_message_channels())
        control_guard.start()
        try:
            yield
        finally:
            shutdown_failures: list[dict[str, str]] = []

            async def record_failure(
                event: str,
                component: str,
                error_type: str,
            ) -> None:
                payload = {"component": component, "error_type": error_type}
                logger.error(
                    "Shutdown lifecycle failure: %s (%s)",
                    component,
                    error_type,
                )
                try:
                    await audit.write(event, "service", payload)
                except BaseException as audit_error:
                    logger.error(
                        "Shutdown audit write failed (%s)",
                        type(audit_error).__name__,
                    )

            async def observe_task(
                task: asyncio.Task[Any] | None,
                component: str,
            ) -> None:
                failure = await _cancel_and_observe_background_task(
                    task,
                    component=component,
                )
                if failure:
                    await record_failure(
                        "shutdown_background_task_failed",
                        failure["component"],
                        failure["error_type"],
                    )

            async def cleanup(component: str, operation) -> None:
                try:
                    await operation()
                except BaseException as exc:
                    failure = {
                        "component": component,
                        "error_type": type(exc).__name__,
                    }
                    shutdown_failures.append(failure)
                    await record_failure(
                        "shutdown_cleanup_failed",
                        failure["component"],
                        failure["error_type"],
                    )

            try:
                await observe_task(channel_bootstrap_task, "message-channel-bootstrap")
                channel_bootstrap_task = None
                await observe_task(feishu_monitor_task, "feishu-connection-monitor")
                feishu_monitor_task = None
                # Stop creators before draining their timers and auxiliary
                # relays/titles; an admitted event may finish during Stop.
                # The security guard stays active throughout this ordering.
                await cleanup("feishu-listener", feishu_bridge.stop_long_connection)
                await cleanup("telegram-poller", telegram_bridge.stop_polling)
                await cleanup("scheduler", scheduler.stop)
                await cleanup("feishu-pending-attachments", pending_feishu_attachments.close)
                await cleanup("telegram-pending-attachments", pending_telegram_attachments.close)
                pending_auxiliary = list(auxiliary_tasks)
                for task in pending_auxiliary:
                    task.cancel()
                if pending_auxiliary:
                    results = await asyncio.gather(
                        *pending_auxiliary,
                        return_exceptions=True,
                    )
                    for task, result in zip(pending_auxiliary, results, strict=True):
                        if isinstance(result, asyncio.CancelledError) or not isinstance(
                            result, BaseException
                        ):
                            continue
                        await record_failure(
                            "shutdown_background_task_failed",
                            task.get_name(),
                            type(result).__name__,
                        )
                # Keep the guard/watchdog alive while every ingress, worker,
                # plugin and delegated runtime drains. Cleanup hooks are model-
                # reachable code too and must remain inside the trust boundary.
                await cleanup("task-manager", manager.shutdown)
                if live_computer_use_tool is not None:
                    await cleanup("live-computer-use", live_computer_use_tool.shutdown)
                await cleanup("mobile-bridge", mobile_bridge.stop)
                await cleanup("openclaw", openclaw_bridge.stop)
            finally:
                # Always perform one final verification and release the durable
                # baseline lease, even when an earlier cleanup hook fails.
                try:
                    control_guard.stop()
                except BaseException as exc:
                    failure = {
                        "component": "control-file-guard",
                        "error_type": type(exc).__name__,
                    }
                    shutdown_failures.append(failure)
                    await record_failure(
                        "shutdown_cleanup_failed",
                        failure["component"],
                        failure["error_type"],
                    )
            if shutdown_failures:
                summary = ", ".join(
                    f"{item['component']} ({item['error_type']})"
                    for item in shutdown_failures
                )
                raise RuntimeError(f"Elren shutdown cleanup failed: {summary}") from None

    app = FastAPI(
        title="Elren",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def redact_request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # Pydantic normally includes the rejected input and validation context in
        # a 422 response. Settings payloads can contain credentials, so expose
        # only enough structure for the UI to identify the invalid field.
        detail = [
            {
                "type": str(error.get("type") or "value_error"),
                "loc": list(error.get("loc") or ()),
                "msg": "Request validation failed",
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    app.add_middleware(LoopbackWebSecurityMiddleware)
    app.state.settings = settings
    app.state.control_guard = control_guard
    app.state.registry = registry
    app.state.manager = manager
    app.state.approvals = approvals
    app.state.human_actions = human_actions
    app.state.vision_runtime = vision_runtime
    app.state.windows_ocr = windows_ocr
    app.state.openclaw_bridge = openclaw_bridge
    app.state.mcp_runtime = mcp_runtime
    app.state.scheduler = scheduler
    app.state.sandbox = sandbox
    app.state.speech_transcriber = speech_transcriber
    app.state.speech_synthesizer = speech_synthesizer
    app.state.runtime_settings = runtime_store
    app.state.provider_secrets = provider_secrets
    app.state.feishu_bridge = feishu_bridge
    app.state.pending_feishu_attachments = pending_feishu_attachments
    app.state.telegram_bridge = telegram_bridge
    app.state.mobile_bridge = mobile_bridge
    app.state.local_model_registry = local_model_registry
    app.state.live_computer_use = live_computer_use_tool

    @app.get("/api/desktop-control/status")
    async def desktop_control_status():
        if live_computer_use_tool is None:
            return {
                "ok": True,
                "available": False,
                "active": False,
                "stop_reason": "unsupported_platform",
                "metrics": {},
            }
        return {"ok": True, **live_computer_use_tool.controller.public_status()}

    @app.post("/api/desktop-control/stop")
    async def stop_desktop_control():
        if live_computer_use_tool is None:
            return {
                "ok": True,
                "stopped": False,
                "status": {
                    "available": False,
                    "active": False,
                    "stop_reason": "unsupported_platform",
                },
            }
        await asyncio.to_thread(
            live_computer_use_tool.controller.emergency_stop,
            "web_emergency_stop",
        )
        current_status = live_computer_use_tool.controller.public_status()
        if current_status.get("active") is not False:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "stopped": False,
                    "error": (
                        "Computer control is still stopping. Press Esc or retry after the current "
                        "input action finishes."
                    ),
                    "status": current_status,
                },
            )
        return {
            "ok": True,
            "stopped": True,
            "status": current_status,
        }

    @app.get("/api/status")
    async def status():
        cloud_vision, local_ocr, paddle_ocr_status, openclaw_status, mcp_status, feishu_status, telegram_status = await asyncio.gather(
            vision_runtime.status(probe=False),
            windows_ocr.status(probe=False),
            paddle_ocr.status(probe=False),
            openclaw_bridge.status(probe_catalog=False, probe_health=False),
            mcp_runtime.status(probe=False),
            feishu_bridge.status(probe=False),
            telegram_bridge.status(probe=False),
        )
        vision_status = {
            **cloud_vision,
            "ready": bool(local_ocr.get("ready") or paddle_ocr_status.get("ready") or cloud_vision.get("ready")),
            "model": "Windows OCR → PaddleOCR AI → verified semantic fallback",
            "source": "privacy-aware-provider-chain",
            "local_ocr": local_ocr,
            "paddle_ocr": paddle_ocr_status,
            "semantic_fallback": cloud_vision,
            "google_fallback": cloud_vision,
        }
        vision_status["model"] = (
            "Windows OCR -> PaddleOCR AI -> verified semantic fallback"
            if IS_WINDOWS
            else "Apple Vision OCR -> PaddleOCR AI -> verified semantic fallback"
        )
        return {
            "ok": True,
            "model": client.model,
            "default_model": runtime_store.value.model,
            "reasoning_effort": runtime_store.value.reasoning_effort,
            "available_models": client.model_options(),
            "local_models": local_model_registry.status(),
            "discussion_team_enabled": runtime_store.value.discussion_team_enabled,
            "discussion_team_leader_model": next(
                (
                    member.model
                    for member in runtime_store.value.discussion_team
                    if member.role == "leader"
                ),
                "auto",
            ),
            "model_providers": provider_secrets.model_provider_status(),
            "workspace": str(settings.workspace),
            "platform": platform_name(),
            **provider_secrets.status(),
            "vision": vision_status,
            "openclaw": openclaw_status,
            "mcp": mcp_status,
            "feishu": feishu_status,
            "telegram": telegram_status,
            "mobile": mobile_bridge.status(),
            "speech": speech_synthesizer.status(),
            "scheduler": await scheduler.run_async(scheduler.stats),
            "sandbox": sandbox.status(),
            "tools": registry.info(),
            "plugin_health": registry.health(),
            "runtime_bundle": portable_runtime_status(settings.workspace),
        }

    @app.get("/api/mobile/status")
    async def mobile_status():
        return mobile_bridge.status()

    @app.post("/api/mobile/pairing")
    async def create_mobile_pairing():
        try:
            return await mobile_bridge.create_pairing()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/mobile/pairing/{pairing_id}/qr.png")
    async def mobile_pairing_qr(pairing_id: str):
        try:
            image = mobile_bridge.pairing_qr_png(pairing_id)
        except KeyError as exc:
            raise HTTPException(404, "Pairing code expired or does not exist") from exc
        return Response(
            image,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.delete("/api/mobile/devices/{device_id}")
    async def revoke_mobile_device(device_id: str):
        # DELETE is intentionally idempotent. The phone can revoke its record
        # first while a settings page still shows the previous status snapshot;
        # a second desktop-side revoke must converge to the same state instead
        # of surfacing a misleading 404 error.
        revoked = mobile_bridge.revoke(device_id)
        return {
            "ok": True,
            "device_id": device_id,
            "revoked": revoked,
            "already_unpaired": not revoked,
        }

    @app.post("/api/speech/synthesize")
    async def synthesize_speech(request: SpeechSynthesisRequest):
        try:
            audio = await speech_synthesizer.synthesize(
                request.text,
                language=request.language,
                voice_name=request.voice_name,
                rate=request.rate,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc
        return FileResponse(
            audio.path,
            media_type=audio.media_type,
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Elren-Speech-Provider": audio.provider,
                "X-Elren-Speech-Voice": quote(audio.voice, safe=" -_."),
            },
        )

    @app.post("/api/speech/stream-sessions")
    async def create_speech_stream_session(request: SpeechSynthesisRequest):
        """Create a short-lived one-shot URL for progressive neural playback."""

        if not speech_synthesizer.status()["online_neural"]:
            raise HTTPException(409, "Neural speech streaming is unavailable")
        now = time.monotonic()
        for session_id, (created_at, _request) in list(speech_stream_sessions.items()):
            if now - created_at > 120:
                speech_stream_sessions.pop(session_id, None)
        session_id = uuid4().hex
        speech_stream_sessions[session_id] = (now, request)
        return {
            "ok": True,
            "stream_url": f"/api/speech/stream/{session_id}",
            "expires_in_seconds": 120,
        }

    @app.get("/api/speech/stream/{session_id}")
    async def stream_speech(session_id: str):
        entry = speech_stream_sessions.pop(session_id, None)
        if not entry or time.monotonic() - entry[0] > 120:
            raise HTTPException(404, "Speech stream expired")
        request = entry[1]
        return StreamingResponse(
            speech_synthesizer.stream_mp3(
                request.text,
                language=request.language,
                voice_name=request.voice_name,
                rate=request.rate,
            ),
            media_type="audio/mpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Elren-Speech-Provider": "edge-neural-stream",
            },
        )

    @app.post("/api/runtimes/probe")
    async def probe_runtimes():
        async def probe_openclaw():
            try:
                await openclaw_bridge.ensure_ready()
            except RuntimeError:
                pass
            return await openclaw_bridge.status(probe_catalog=True)

        cloud_vision, local_ocr, paddle_ocr_status, openclaw_status, mcp_status, feishu_status, telegram_status, local_models_status = await asyncio.gather(
            vision_runtime.status(probe=True),
            windows_ocr.status(probe=True),
            paddle_ocr.status(probe=True),
            probe_openclaw(),
            mcp_runtime.status(probe=True),
            feishu_bridge.status(probe=True),
            telegram_bridge.status(probe=True),
            refresh_local_models(),
        )
        # Local-model discovery can complete during the parallel probe and may
        # have selected a declared visual model. Read the final semantic state
        # once more so this response never reports the pre-discovery snapshot.
        cloud_vision = await vision_runtime.status(probe=False)
        vision_status = {
            **cloud_vision,
            "ready": bool(local_ocr.get("ready") or paddle_ocr_status.get("ready") or cloud_vision.get("ready")),
            "model": "Windows OCR → PaddleOCR AI → verified semantic fallback",
            "source": "privacy-aware-provider-chain",
            "local_ocr": local_ocr,
            "paddle_ocr": paddle_ocr_status,
            "semantic_fallback": cloud_vision,
            "google_fallback": cloud_vision,
        }
        vision_status["model"] = (
            "Windows OCR -> PaddleOCR AI -> verified semantic fallback"
            if IS_WINDOWS
            else "Apple Vision OCR -> PaddleOCR AI -> verified semantic fallback"
        )
        return {"vision": vision_status, "local_models": local_models_status, "available_models": client.model_options(), "openclaw": openclaw_status, "mcp": mcp_status, "feishu": feishu_status, "telegram": telegram_status, "runtime_bundle": portable_runtime_status(settings.workspace)}

    @app.get("/api/mcp/status")
    async def mcp_status():
        return await mcp_runtime.status(probe=True)

    @app.get("/api/mcp/tools")
    async def mcp_tools():
        try:
            return await mcp_runtime.list_tools()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/sandbox/status")
    async def sandbox_status():
        return sandbox.status()

    @app.get("/api/settings")
    async def get_settings():
        key_status = provider_secrets.status()
        available_selectors = {
            item["selector"] for item in client.model_options()
        }
        stored_model = runtime_store.value.model
        displayed_model = (
            stored_model
            if stored_model == "auto" or stored_model in available_selectors
            else "auto"
        )
        context_selector = (
            client.model
            if displayed_model == "auto"
            else displayed_model
        )
        return {
            "settings_api_version": 2,
            **runtime_store.value.model_dump(mode="json"),
            "model": displayed_model,
            "active_model": client.model,
            "stored_model_unavailable": (
                stored_model if displayed_model != stored_model else ""
            ),
            **client.context_window_info(context_selector),
            "base_url": client.base_url,
            "available_models": client.model_options(),
            "local_models": local_model_registry.status(),
            "model_providers": provider_secrets.model_provider_status(),
            "workspace": str(settings.workspace),
            "host": settings.deepdesk_host,
            "port": settings.deepdesk_port,
            "system_voice_language": system_speech_language(),
            **key_status,
            "vision_key_configured": key_status["gemini_key_configured"],
        }

    def safe_settings_state() -> dict[str, Any]:
        state = runtime_store.value.model_dump(mode="json")
        state.update(provider_secrets.status())
        return state

    def same_origin_browser_request(request: Request) -> bool:
        """Distinguish this app's browser pages from native/no-Origin callers."""

        origin = request.headers.get("origin", "").strip()
        fetch_site = request.headers.get("sec-fetch-site", "").strip().casefold()
        host = request.headers.get("host", "").strip().casefold()
        if not origin or fetch_site != "same-origin" or not host:
            return False
        try:
            parsed = urlsplit(origin)
            if parsed.scheme.casefold() not in {"http", "https"}:
                return False
            if parsed.scheme.casefold() != request.url.scheme.casefold():
                return False
            if parsed.username is not None or parsed.password is not None:
                return False
            _ = parsed.port
        except ValueError:
            return False
        return parsed.netloc.casefold() == host

    def prospective_model_options(request: RuntimeSettingsPatch) -> list[dict[str, Any]]:
        """Build the model catalog a settings transaction would create.

        The UI saves a newly entered credential/provider row and its selected
        default model in one PATCH.  Validate against that prospective catalog,
        not the stale live client, while still keeping validation ahead of any
        credential write.
        """

        current = provider_secrets.value

        def selected(candidate: str | None, existing: str) -> str:
            value = str(candidate or "").strip()
            return value or existing

        preview = DeepSeekClient(
            base_url=client.base_url,
            model=client.model,
            primary_key=selected(request.deepseek_api_key, current.deepseek_primary),
            backup_key=selected(
                request.deepseek_backup_api_key,
                current.deepseek_backup,
            ),
            timeout=client.timeout,
            max_output_tokens=client.max_output_tokens,
        )
        relay_key = selected(request.aicodemirror_api_key, current.aicodemirror)
        preview.set_provider_models(
            provider_secrets.preview_model_providers(request.model_providers),
            {
                "anthropic": relay_key,
                "openai": relay_key,
                "google": relay_key,
            },
            selected(
                request.aicodemirror_fable_api_key,
                current.aicodemirror_fable,
            ),
        )
        options = preview.model_options()
        # Local discovery is independent of provider credential updates. Keep
        # its already-probed selectors available during this pure preview.
        options.extend(
            item
            for item in client.model_options()
            if item.get("provider") == "local"
        )
        return options

    def _prepare_settings_patch(
        request: RuntimeSettingsPatch,
        *,
        allow_ai_control_settings: bool = False,
    ) -> RuntimeSettingsPatch:
        """Reject invalid candidates before opening the durable transaction."""
        ai_control_fields = {
            "custom_system_prompt_suffix",
            "discussion_team_enabled",
            "discussion_team",
        }
        if request.model_fields_set & ai_control_fields:
            if not allow_ai_control_settings:
                raise PermissionError(
                    "AI control prompts and discussion-team instructions can only be changed "
                    "from the local Settings screen"
                )
            if not control_guard.local_ui_control_update_allowed:
                raise PermissionError(
                    "AI control settings cannot be changed while an Agent tool is controlling "
                    "the local interface"
                )
        # Resolve model names before writing any settings or credentials.  In
        # particular, never turn an ASR near miss such as GPT5.6SOU into auto.
        options = prospective_model_options(request)
        if request.model is not None:
            resolved_model = resolve_model_selection(
                request.model, options
            )
            request = request.model_copy(update={"model": resolved_model})
        runtime_updates: dict[str, Any] = {
            "model": request.model or runtime_store.value.model,
        }
        request = request.model_copy(update=runtime_updates)
        candidate = runtime_store.preview(request)
        if request.model_fields_set & ai_control_fields:
            # Same serialized baseline budget as ControlFileGuard acceptance;
            # the individual participant limits alone do not bound a roster.
            encoded_control = json.dumps({
                "custom_system_prompt_suffix": candidate.custom_system_prompt_suffix,
                "discussion_team_enabled": candidate.discussion_team_enabled,
                "discussion_team": [member.model_dump(mode="json") for member in candidate.discussion_team],
            }, ensure_ascii=True, sort_keys=True)
            if len(encoded_control) > 520_000:
                raise SettingsCandidateError("AI control settings exceed the supported storage size")
        return request

    def _apply_settings_patch_body(
        request: RuntimeSettingsPatch,
        *,
        activations: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        # Deliberately synchronous: no cancellation point or event-loop reader
        # can observe the in-progress local file/live-state transaction.
        ai_control_fields = {
            "custom_system_prompt_suffix", "discussion_team_enabled", "discussion_team",
        }
        before = safe_settings_state()
        previous_feishu_credentials = (
            provider_secrets.value.feishu_app_id,
            provider_secrets.value.feishu_app_secret,
        )
        previous_telegram_token = provider_secrets.value.telegram_bot_token
        updated_secrets = provider_secrets.update(
            deepseek_primary=request.deepseek_api_key,
            deepseek_backup=request.deepseek_backup_api_key,
            gemini=request.gemini_api_key,
            pollinations=request.pollinations_api_key,
            huggingface=request.huggingface_token,
            aicodemirror=request.aicodemirror_api_key,
            aicodemirror_fable=request.aicodemirror_fable_api_key,
            model_providers=request.model_providers,
            feishu_app_id=request.feishu_app_id,
            feishu_app_secret=request.feishu_app_secret,
            feishu_open_id=request.feishu_open_id,
            telegram_bot_token=request.telegram_bot_token,
            telegram_chat_id=request.telegram_chat_id,
            github_token=request.github_token,
            google_places_api_key=request.google_places_api_key,
            trello_api_key=request.trello_api_key,
            trello_token=request.trello_token,
            elevenlabs_api_key=request.elevenlabs_api_key,
            notion_token=request.notion_token,
            spotify_client_id=request.spotify_client_id,
            spotify_client_secret=request.spotify_client_secret,
            op_service_account_token=request.op_service_account_token,
            giphy_api_key=request.giphy_api_key,
            tenor_api_key=request.tenor_api_key,
            apify_api_token=request.apify_api_token,
            firecrawl_api_key=request.firecrawl_api_key,
            eightctl_email=request.eightctl_email,
            eightctl_password=request.eightctl_password,
            deliveroo_bearer_token=request.deliveroo_bearer_token,
            deliveroo_cookie=request.deliveroo_cookie,
            things_auth_token=request.things_auth_token,
            sag_api_key=request.sag_api_key,
        )
        client.set_keys(updated_secrets.deepseek_primary, updated_secrets.deepseek_backup)
        client.set_provider_models(
            updated_secrets.model_providers,
            {
                "anthropic": updated_secrets.aicodemirror,
                "openai": updated_secrets.aicodemirror,
                "google": updated_secrets.aicodemirror,
            },
            updated_secrets.aicodemirror_fable,
        )
        vision_runtime.set_api_keys(
            updated_secrets.gemini,
            updated_secrets.aicodemirror,
        )
        if vision_runtime.configured:
            activations.append(("vision", lambda: spawn_auxiliary(vision_runtime.discover(), name="vision-settings-probe")))
        media_generator.set_api_key(updated_secrets.pollinations)
        media_generator.set_huggingface_token(updated_secrets.huggingface)
        feishu_bridge.set_credentials(
            app_id=updated_secrets.feishu_app_id,
            app_secret=updated_secrets.feishu_app_secret,
            verification_token=updated_secrets.feishu_verification_token,
            encrypt_key=updated_secrets.feishu_encrypt_key,
            default_receive_id=updated_secrets.feishu_open_id,
        )
        current_feishu_credentials = (
            updated_secrets.feishu_app_id,
            updated_secrets.feishu_app_secret,
        )
        if current_feishu_credentials != previous_feishu_credentials:
            activations.append(("feishu", feishu_bridge.restart_long_connection))
        telegram_bridge.set_credentials(
            updated_secrets.telegram_bot_token,
            updated_secrets.telegram_chat_id,
        )
        if updated_secrets.telegram_bot_token != previous_telegram_token:
            activations.append(("telegram", telegram_bridge.restart_polling))
        openclaw_runtime_key = (
            updated_secrets.openclaw_deepseek_api_key
            or updated_secrets.deepseek_primary
        )
        openclaw_bridge.provider_env = (
            {"DEEPSEEK_API_KEY": openclaw_runtime_key}
            if openclaw_runtime_key
            else {}
        )
        activations.append(("openclaw", lambda: openclaw_bridge.set_skill_credentials(
            {skill: tool_credentials.environment_for(skill) for skill in SKILL_CREDENTIAL_ENV}
        )))
        with control_guard.runtime_settings_transaction():
            updated = runtime_store.update(request)
            if request.model_fields_set & ai_control_fields:
                control_guard.accept_runtime_ai_control_settings(
                    custom_system_prompt_suffix=updated.custom_system_prompt_suffix,
                    discussion_team_enabled=updated.discussion_team_enabled,
                    discussion_team=[
                        member.model_dump(mode="json") for member in updated.discussion_team
                    ],
                )
        client.set_default_model_preference(updated.model)
        engine.max_steps = None
        client.timeout = updated.request_timeout
        client.max_output_tokens = updated.max_output_tokens
        response = {
            "settings_api_version": 2,
            **updated.model_dump(mode="json"),
            "active_model": client.model,
            "system_voice_language": system_speech_language(),
            **provider_secrets.status(),
            "available_models": client.model_options(),
            "model_providers": provider_secrets.model_provider_status(),
            **client.context_window_info(client.model),
        }
        after = safe_settings_state()
        configured_map = {
            "deepseek_api_key": "primary_key_configured",
            "deepseek_backup_api_key": "backup_key_configured",
            "gemini_api_key": "gemini_key_configured",
            "pollinations_api_key": "pollinations_key_configured",
            "huggingface_token": "huggingface_token_configured",
            "aicodemirror_api_key": "aicodemirror_key_configured",
            "aicodemirror_fable_api_key": "aicodemirror_fable_key_configured",
            "feishu_app_id": "feishu_configured",
            "feishu_app_secret": "feishu_configured",
            "feishu_open_id": "feishu_open_id_configured",
            "telegram_bot_token": "telegram_configured",
            "telegram_chat_id": "telegram_chat_id_configured",
            "github_token": "github_token_configured",
            "google_places_api_key": "google_places_key_configured",
            "trello_api_key": "trello_configured",
            "trello_token": "trello_configured",
            "elevenlabs_api_key": "elevenlabs_key_configured",
            "notion_token": "notion_token_configured",
            "spotify_client_id": "spotify_configured",
            "spotify_client_secret": "spotify_configured",
            "op_service_account_token": "onepassword_configured",
            "giphy_api_key": "giphy_key_configured",
            "tenor_api_key": "tenor_key_configured",
            "apify_api_token": "apify_token_configured",
            "firecrawl_api_key": "firecrawl_key_configured",
            "eightctl_email": "eightctl_email_configured",
            "eightctl_password": "eightctl_password_configured",
            "deliveroo_bearer_token": "deliveroo_token_configured",
            "deliveroo_cookie": "deliveroo_cookie_configured",
            "things_auth_token": "things_token_configured",
            "sag_api_key": "sag_alt_key_configured",
            "model_providers": "model_provider_count",
        }
        changed: dict[str, dict[str, Any]] = {}
        for field in request.model_fields_set:
            status_field = configured_map.get(field)
            if status_field:
                if status_field == "model_provider_count":
                    before_label = f"{int(before.get(status_field) or 0)} configured"
                    after_label = f"{int(after.get(status_field) or 0)} configured"
                else:
                    before_label = "configured (hidden)" if before.get(status_field) else "not configured"
                    after_label = "configured (hidden)" if after.get(status_field) else "not configured"
                # Explicit credential/provider writes may replace a hidden value
                # without changing its configured/count status, so retain them in
                # the evidence while never revealing either plaintext value.
                changed[field] = {"before": before_label, "after": after_label}
            elif field in after and before.get(field) != after.get(field):
                changed[field] = {"before": before.get(field), "after": after.get(field)}
        response["changed"] = changed
        return response

    async def _apply_settings_patch_unlocked(
        request: RuntimeSettingsPatch,
        *,
        allow_ai_control_settings: bool = False,
    ) -> dict[str, Any]:
        request = _prepare_settings_patch(
            request, allow_ai_control_settings=allow_ai_control_settings,
        )
        targets = {name: path for name, path in recovery_targets.items() if name != "control"}
        if control_guard._persistent:
            targets["control"] = control_guard.state_path
        transaction = SettingsTransaction(settings.data_dir, targets, provider_secrets.protector)
        objects = (provider_secrets, runtime_store, client, vision_runtime, media_generator,
                   feishu_bridge, telegram_bridge,
                   openclaw_bridge, engine)
        snapshots = [(obj, copy_settings_state(vars(obj))) for obj in objects]
        control_snapshot = {name: copy_settings_state(getattr(control_guard, name)) for name in (
            "_trusted_runtime_control", "_trusted_state_bytes", "_state_parent_identity",
        )}
        activations: list[tuple[str, Any]] = []
        # No awaited I/O in the body: external activation is queued until the
        # durable commit receipt. Readers on the event loop see old or new, not
        # the transient rollback candidate. The guard lock also excludes its
        # background watchdog while preferences/control baseline are changing.
        with control_guard.runtime_settings_transaction(), provider_secrets._update_lock, runtime_store._update_lock:
            try:
                transaction.begin()
                response = _apply_settings_patch_body(request, activations=activations)
                transaction.commit()
            except BaseException:
                for obj, snapshot in snapshots:
                    vars(obj).clear()
                    vars(obj).update(snapshot)
                for name, value in control_snapshot.items():
                    setattr(control_guard, name, value)
                try:
                    transaction.rollback()
                except Exception:
                    raise SettingsRecoveryError(
                        "Settings were not committed; recovery must finish before further settings changes"
                    ) from None
                raise
        response["saved"] = True
        warnings: list[dict[str, str]] = []
        pending: list[str] = []
        for name, activate in activations:
            try:
                outcome = activate()
                if asyncio.iscoroutine(outcome):
                    outcome = await outcome
                if isinstance(outcome, asyncio.Future):
                    # Background probes intentionally do not delay a save.
                    # Do not describe a queued probe as successful activation.
                    pending.append(name)
                if outcome is False and name in {"feishu", "telegram"}:
                    warnings.append({"component": name, "code": "activation_not_ready"})
            except Exception:
                # External startup is not an atomic local file operation. A
                # failed reconnect must not turn a committed save into HTTP500.
                warnings.append({"component": name, "code": "activation_failed"})
        response["activation_warnings"] = warnings
        response["activation_pending"] = pending
        return response

    async def apply_settings_patch(
        request: RuntimeSettingsPatch,
        *,
        allow_ai_control_settings: bool = False,
    ) -> dict[str, Any]:
        # One PATCH spans runtime preferences, provider credentials, live model
        # routing and channel restarts.  Per-file locks alone allow two callers
        # (web + voice/Telegram, for example) to interleave those phases and
        # return a response describing the other caller's state.  Serialize the
        # complete transaction at the app boundary.
        async with settings_update_lock:
            return await _apply_settings_patch_unlocked(
                request,
                allow_ai_control_settings=allow_ai_control_settings,
            )

    async def apply_remote_settings(changes: dict[str, Any]) -> dict[str, Any]:
        try:
            return await apply_settings_patch(RuntimeSettingsPatch.model_validate(changes))
        except ModelSelectionError as exc:
            return {
                "updated": False,
                "confirmation_required": True,
                **exc.payload(),
            }

    settings_update_ref["apply"] = apply_remote_settings

    @app.patch("/api/settings")
    async def update_settings(request: RuntimeSettingsPatch, http_request: Request):
        ai_control_fields = {
            "custom_system_prompt_suffix",
            "discussion_team_enabled",
            "discussion_team",
        }
        if request.model_fields_set & ai_control_fields and not same_origin_browser_request(
            http_request
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "AI control prompts and discussion-team instructions require a "
                    "same-origin browser Settings request"
                ),
            )
        try:
            return await apply_settings_patch(
                request,
                allow_ai_control_settings=True,
            )
        except ModelSelectionError as exc:
            raise HTTPException(status_code=422, detail=exc.payload()) from exc
        except SettingsCandidateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SettingsRecoveryError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def deliver_feishu_task_result(
        task, receive_id: str, receive_id_type: str
    ) -> bool:
        result = (
            normalize_assistant_display_text(task.result)
            if task.result
            else task.error or "任务已结束，但没有可发送的结果。"
        )
        try:
            delivery = await feishu_bridge.send_text(
                receive_id,
                f"Elren 任务 {task.id} 已结束：\n{str(result)[:3800]}",
                receive_id_type=receive_id_type,
                max_attempts=5,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await manager.emit_async(task, "feishu_delivery", {"status": "failed", "error": error})
            await audit.write("feishu_delivery_failed", task.id, {"error": error})
            return False
        image_paths = feishu_result_image_paths(task)
        image_deliveries: list[dict[str, str]] = []
        image_errors: list[dict[str, str]] = []
        for image_path in image_paths:
            try:
                image_delivery = await feishu_bridge.send_image(
                    receive_id,
                    image_path,
                    receive_id_type=receive_id_type,
                    max_attempts=5,
                )
                image_deliveries.append(
                    {
                        "path": str(image_path),
                        "message_id": str(image_delivery.get("message_id") or ""),
                    }
                )
            except Exception as exc:
                image_errors.append(
                    {"path": str(image_path), "error": f"{type(exc).__name__}: {exc}"}
                )
        document_paths = feishu_result_document_paths(task)
        file_deliveries: list[dict[str, str]] = []
        file_errors: list[dict[str, str]] = []
        for document_path in document_paths:
            try:
                file_delivery = await feishu_bridge.send_file(
                    receive_id,
                    document_path,
                    receive_id_type=receive_id_type,
                    max_attempts=5,
                )
                file_deliveries.append(
                    {
                        "path": str(document_path),
                        "message_id": str(file_delivery.get("message_id") or ""),
                    }
                )
            except Exception as exc:
                file_errors.append(
                    {"path": str(document_path), "error": f"{type(exc).__name__}: {exc}"}
                )
        delivery_errors = image_errors + file_errors
        await manager.emit_async(
            task,
            "feishu_delivery",
            {
                "status": "delivered" if not delivery_errors else "attachment_delivery_failed",
                "message_id": delivery.get("message_id", ""),
                "images": image_deliveries,
                "image_errors": image_errors,
                "files": file_deliveries,
                "file_errors": file_errors,
            },
        )
        await audit.write(
            "feishu_delivery_succeeded" if not delivery_errors else "feishu_attachment_delivery_failed",
            task.id,
            {
                "message_id": delivery.get("message_id", ""),
                "images": image_deliveries,
                "image_errors": image_errors,
                "files": file_deliveries,
                "file_errors": file_errors,
            },
        )
        return not delivery_errors

    def feishu_result_image_paths(task) -> list[Path]:
        """Return only images explicitly referenced by the final answer.

        Input attachments and internal observation screenshots are deliberately
        not scanned or echoed automatically.
        """
        return _referenced_output_paths(
            task,
            settings.workspace,
            ("jpg", "jpeg", "png", "webp", "gif", "bmp", "ico", "tif", "tiff", "heic"),
            max_bytes=10 * 1024 * 1024,
        )

    def feishu_result_document_paths(task) -> list[Path]:
        """Return supported document/audio/video files referenced by the final answer."""
        return _referenced_output_paths(
            task,
            settings.workspace,
            (
                "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt",
                "mp3", "wav", "flac", "m4a", "aac", "ogg", "opus",
                "mp4", "webm", "mov", "mkv",
            ),
            max_bytes=30 * 1024 * 1024,
        )

    async def relay_feishu_task(task, receive_id: str, receive_id_type: str) -> None:
        while task.status in {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.WAITING,
            TaskStatus.WAITING_USER,
        }:
            await asyncio.sleep(1)
        await deliver_feishu_task_result(task, receive_id, receive_id_type)

    async def recover_interrupted_feishu_tasks() -> None:
        for previous in await run_owned_thread(task_store.list_restart_interrupted, "feishu"):
            interruption = str(previous.error or "")
            if (
                previous.status != TaskStatus.FAILED
                or not interruption.startswith("Elren 重启时任务仍在运行")
            ):
                continue
            try:
                receive_id, receive_id_type = manager.remote_delivery_route(previous)
            except ValueError:
                logger.warning(
                    "Skipped interrupted Feishu task %s without an immutable reply route",
                    previous.id,
                )
                continue
            manager.tasks.setdefault(previous.id, previous)
            resumed = await manager.continue_from_async(
                previous,
                "服务重启后自动恢复：继续完成原始飞书要求，并将最终结果回传给用户。",
                ApprovalPolicy.AUTONOMOUS,
                previous.agent_profile,
                model_preference=previous.model_preference,
                reasoning_effort=previous.reasoning_effort,
                discussion_team_enabled=previous.discussion_team_enabled,
                discussion_team=[
                    member.model_copy(deep=True)
                    for member in previous.discussion_team
                ],
            )
            await manager.mark_recovered_async(previous, resumed.id)
            try:
                await feishu_bridge.send_text(
                    receive_id,
                    f"Elren 已在重启后自动恢复任务：{resumed.id}",
                    receive_id_type=receive_id_type,
                )
            except Exception:
                logger.debug("Failed to announce restored Feishu task", exc_info=True)
            spawn_auxiliary(
                relay_feishu_task(resumed, receive_id, receive_id_type),
                name=f"relay-feishu-{resumed.id}",
            )

    async def launch_feishu_task(
        prompt: str,
        attachments: list[str],
        receive_id: str,
        receive_id_type: str,
        *,
        acknowledge: bool = True,
    ) -> dict:
        task_snapshot = remote_channel_task_snapshot(
            runtime_store.value,
            automatic_model=client.model,
        )
        if not client.has_model_credentials(task_snapshot["model_preference"]):
            message = "尚未配置模型 API 密钥，请先在 Elren 的“设置 → 模型与密钥”中配置。"
            try:
                await feishu_bridge.send_text(
                    receive_id, message, receive_id_type=receive_id_type
                )
            except Exception:
                logger.debug("Failed to report missing model key to Feishu", exc_info=True)
            return {"ok": False, "reason": "model_api_key_required"}
        task = await manager.create_async(
            prompt,
            ApprovalPolicy.AUTONOMOUS,
            AgentProfile.GENERAL,
            attachments=attachments,
            source="feishu",
            remote_recipient_id=receive_id,
            remote_recipient_type=receive_id_type,
            **task_snapshot,
        )
        if acknowledge:
            try:
                await feishu_bridge.send_text(
                    receive_id,
                    f"已收到你的远程要求，Elren 正在执行。任务 ID：{task.id}",
                    receive_id_type=receive_id_type,
                )
            except Exception:
                logger.debug("Failed to acknowledge Feishu task", exc_info=True)
        spawn_auxiliary(
            relay_feishu_task(task, receive_id, receive_id_type),
            name=f"relay-feishu-{task.id}",
        )
        return {
            "ok": True,
            "task_id": task.id,
            "attachment_count": len(attachments),
        }

    def attachment_only_prompt(attachments: list[str], image_count: int) -> str:
        if attachments and image_count == len(attachments):
            return "请读取并详细处理我从飞书发送的图片；若图片内没有明确指令，请描述和分析图片内容。"
        return "请读取并处理我从飞书发送的附件。"

    async def flush_pending_feishu_attachments(entry: dict) -> None:
        metadata = entry["metadata"]
        attachments = entry["attachments"]
        reservations = [
            (str(item.get("event_id") or ""), str(item.get("token") or ""))
            for item in metadata.get("_event_reservations") or []
            if isinstance(item, dict)
        ]
        committed = False
        try:
            result = await launch_feishu_task(
                attachment_only_prompt(
                    attachments, int(metadata.get("image_count") or 0)
                ),
                attachments,
                str(metadata["receive_id"]),
                str(metadata["receive_id_type"]),
                acknowledge=False,
            )
            if not result.get("ok"):
                return
            if not feishu_bridge.commit_event_ids(reservations):
                raise RuntimeError(
                    "Pending Feishu event reservation expired before task creation"
                )
            committed = True
        finally:
            if not committed:
                feishu_bridge.rollback_event_ids(reservations)

    async def accept_feishu_event(event: dict) -> dict:
        # An authenticated delivery may already have created work when its
        # HTTP caller disconnects. Own admission through lease commit/rollback;
        # cancellation is not proof that no task was created.
        return await run_owned_async(commit_feishu_event, event)

    async def commit_feishu_event(event: dict) -> dict:
        if not feishu_bridge.configured:
            raise HTTPException(409, "飞书尚未配置 app_id 和 app_secret")
        prompt = str(event.get("prompt") or "").strip()
        resources = event.get("resources") or []
        receive_id = str(event.get("receive_id") or "").strip()
        receive_id_type = str(event.get("receive_id_type") or "open_id").strip()
        if (
            not (prompt or resources)
            or not receive_id
            or receive_id_type not in {"open_id", "chat_id"}
        ):
            raise HTTPException(400, "飞书长连接事件格式无效")
        event_id = str(event.get("event_id") or "")
        # A reservation blocks overlapping HTTP/WebSocket deliveries without
        # consuming the platform retry. It becomes durable only after the
        # attachment/task pipeline reports successful local acceptance.
        try:
            reservation = feishu_bridge.reserve_event_id(event_id)
        except RuntimeError as exc:
            raise HTTPException(
                503, "飞书事件处理队列暂时繁忙，请稍后重试"
            ) from exc
        if reservation is None:
            return {"ok": True, "ignored": True, "reason": "duplicate_event"}
        committed = False
        transferred_to_pending = False
        pending_reservations: list[tuple[str, str]] = []
        try:
            # An open_id is application-specific. An authenticated event is the
            # authoritative way to learn the current app's ID for this user.
            if receive_id_type == "open_id" and receive_id != feishu_bridge.default_receive_id:
                provider_secrets.update(feishu_open_id=receive_id)
                feishu_bridge.remember_default_receive_id(receive_id)
            attachments: list[str] = []
            image_count = 0
            if resources:
                safe_event_id = re.sub(
                    r"[^A-Za-z0-9_-]+", "_", event_id or uuid4().hex
                )
                destination = settings.screenshot_dir / "uploads" / "feishu" / safe_event_id
                for resource in resources[:20]:
                    try:
                        attachment = await feishu_bridge.download_message_resource(
                            str(event.get("message_id") or ""),
                            str(resource.get("file_key") or ""),
                            str(resource.get("type") or ""),
                            destination,
                            file_name=str(resource.get("file_name") or ""),
                            max_bytes=(
                                100 * 1024 * 1024
                                if str(resource.get("type") or "")
                                in {"audio", "media", "video"}
                                else 25 * 1024 * 1024
                            ),
                        )
                    except Exception as exc:
                        await feishu_bridge.send_text(
                            receive_id,
                            f"Elren 未能读取你发送的附件：{type(exc).__name__}: {exc}",
                            receive_id_type=receive_id_type,
                        )
                        return {"ok": False, "reason": "attachment_download_failed"}
                    attachments.append(str(attachment))
                image_count = sum(
                    str(resource.get("type") or "") == "image" for resource in resources
                )

            conversation_key = f"{receive_id_type}:{receive_id}"
            if attachments and not prompt:
                existing_pending = pending_feishu_attachments._pending.get(
                    conversation_key
                )
                carried_reservations = list(
                    (
                        (existing_pending or {}).get("metadata") or {}
                    ).get("_event_reservations")
                    or []
                )
                first_attachment = await pending_feishu_attachments.stage(
                    conversation_key,
                    attachments,
                    {
                        "receive_id": receive_id,
                        "receive_id_type": receive_id_type,
                        "image_count": image_count,
                        "_event_reservations": [
                            *carried_reservations,
                            {"event_id": event_id, "token": reservation},
                        ],
                    },
                    flush_pending_feishu_attachments,
                )
                if first_attachment:
                    try:
                        await feishu_bridge.send_text(
                            receive_id,
                            "已收到你的远程要求。附件正在接收并等待后续说明；若 15 秒内没有补充，将自动开始处理。",
                            receive_id_type=receive_id_type,
                        )
                    except Exception:
                        logger.debug(
                            "Failed to acknowledge pending Feishu attachment",
                            exc_info=True,
                        )
                # The timer or a following text event now owns this lease. It
                # will commit only after manager.create succeeds, or roll back
                # if task creation fails.
                transferred_to_pending = True
                return {
                    "ok": True,
                    "pending_attachments": True,
                    "attachment_count": len(attachments),
                }
            else:
                already_acknowledged = False
                if prompt:
                    pending = pending_feishu_attachments.take(conversation_key)
                    if pending:
                        attachments = [*pending["attachments"], *attachments]
                        attachments = list(dict.fromkeys(attachments))
                        pending_reservations = [
                            (
                                str(item.get("event_id") or ""),
                                str(item.get("token") or ""),
                            )
                            for item in (
                                (pending.get("metadata") or {}).get(
                                    "_event_reservations"
                                )
                                or []
                            )
                            if isinstance(item, dict)
                        ]
                        already_acknowledged = True

                result = await launch_feishu_task(
                    prompt or attachment_only_prompt(attachments, image_count),
                    attachments,
                    receive_id,
                    receive_id_type,
                    acknowledge=not already_acknowledged,
                )
            if not result.get("ok"):
                return result
            if not feishu_bridge.commit_event_ids(
                [*pending_reservations, (event_id, reservation)]
            ):
                raise RuntimeError(
                    "Feishu event reservation expired before it could be committed"
                )
            committed = True
            return result
        finally:
            if not committed and not transferred_to_pending:
                feishu_bridge.rollback_event_ids(
                    [*pending_reservations, (event_id, reservation)]
                )

    async def deliver_telegram_task_result(task, chat_id: str) -> bool:
        result = (
            normalize_assistant_display_text(task.result)
            if task.result
            else task.error or "任务已结束，但没有可发送的结果。"
        )
        try:
            delivery = await telegram_bridge.send_text(
                chat_id,
                f"Elren 任务 {task.id} 已结束：\n{result!s}",
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await manager.emit_async(task, "telegram_delivery", {"status": "failed", "error": error})
            await audit.write("telegram_delivery_failed", task.id, {"error": error})
            return False
        files = [*feishu_result_image_paths(task), *feishu_result_document_paths(task)]
        file_deliveries: list[dict[str, str]] = []
        file_errors: list[dict[str, str]] = []
        for path in files:
            try:
                sent = await telegram_bridge.send_file(chat_id, path)
                file_deliveries.append(
                    {"path": str(path), "message_id": str(sent.get("message_id") or "")}
                )
            except Exception as exc:
                file_errors.append(
                    {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
                )
        await manager.emit_async(
            task,
            "telegram_delivery",
            {
                "status": "delivered" if not file_errors else "attachment_delivery_failed",
                "message_id": delivery.get("message_id", ""),
                "files": file_deliveries,
                "file_errors": file_errors,
            },
        )
        await audit.write(
            "telegram_delivery_succeeded" if not file_errors else "telegram_attachment_delivery_failed",
            task.id,
            {"message_id": delivery.get("message_id", ""), "files": file_deliveries, "file_errors": file_errors},
        )
        return not file_errors

    async def relay_telegram_task(task, chat_id: str) -> None:
        while task.status in {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.WAITING,
            TaskStatus.WAITING_USER,
        }:
            await asyncio.sleep(1)
        await deliver_telegram_task_result(task, chat_id)

    async def recover_interrupted_telegram_tasks() -> None:
        for previous in await run_owned_thread(task_store.list_restart_interrupted, "telegram"):
            interruption = str(previous.error or "")
            if (
                previous.status != TaskStatus.FAILED
                or not interruption.startswith("Elren 重启时任务仍在运行")
            ):
                continue
            try:
                chat_id, _recipient_type = manager.remote_delivery_route(previous)
            except ValueError:
                logger.warning(
                    "Skipped interrupted Telegram task %s without an immutable reply route",
                    previous.id,
                )
                continue
            manager.tasks.setdefault(previous.id, previous)
            resumed = await manager.continue_from_async(
                previous,
                "服务重启后自动恢复：继续完成原始 Telegram 要求，并将最终结果回传给用户。",
                ApprovalPolicy.AUTONOMOUS,
                previous.agent_profile,
                model_preference=previous.model_preference,
                reasoning_effort=previous.reasoning_effort,
                discussion_team_enabled=previous.discussion_team_enabled,
                discussion_team=[
                    member.model_copy(deep=True)
                    for member in previous.discussion_team
                ],
            )
            await manager.mark_recovered_async(previous, resumed.id)
            try:
                await telegram_bridge.send_text(
                    chat_id, f"Elren 已在重启后自动恢复任务：{resumed.id}"
                )
            except Exception as exc:
                log_telegram_delivery_failure(
                    "Failed to announce restored Telegram task", exc
                )
            spawn_auxiliary(
                relay_telegram_task(resumed, chat_id),
                name=f"relay-telegram-{resumed.id}",
            )

    async def launch_telegram_task(
        prompt: str,
        attachments: list[str],
        chat_id: str,
        *,
        acknowledge: bool = True,
        voice_request: bool = False,
    ) -> dict[str, Any]:
        task_snapshot = remote_channel_task_snapshot(
            runtime_store.value,
            automatic_model=client.model,
        )
        if not client.has_model_credentials(task_snapshot["model_preference"]):
            try:
                await telegram_bridge.send_text(
                    chat_id,
                    "尚未配置模型 API 密钥，请先在 Elren 的“设置 → 模型与密钥”中配置。",
                )
            except Exception as exc:
                log_telegram_delivery_failure(
                    "Failed to report missing model key to Telegram", exc
                )
            # The user has been notified and replaying this Telegram update
            # cannot succeed until Settings changes. Consume it to avoid a
            # permanent retry/notification loop.
            return {
                "ok": False,
                "reason": "model_api_key_required",
                "ack_update": True,
            }
        task = await manager.create_async(
            prompt,
            ApprovalPolicy.AUTONOMOUS,
            AgentProfile.GENERAL,
            attachments=attachments,
            source="telegram",
            remote_recipient_id=chat_id,
            remote_recipient_type="chat_id",
            voice_request=voice_request,
            **task_snapshot,
        )
        if acknowledge:
            try:
                await telegram_bridge.send_text(
                    chat_id,
                    f"已收到你的远程要求，Elren 正在执行。任务 ID：{task.id}",
                )
            except Exception as exc:
                log_telegram_delivery_failure(
                    "Failed to acknowledge Telegram task", exc
                )
        spawn_auxiliary(
            relay_telegram_task(task, chat_id),
            name=f"relay-telegram-{task.id}",
        )
        return {
            "ok": True,
            "task_id": task.id,
            "attachment_count": len(attachments),
            "ack_update": True,
        }

    def telegram_attachment_only_prompt(attachments: list[str], image_count: int) -> str:
        if attachments and image_count == len(attachments):
            return "请读取并详细处理我从 Telegram 发送的图片；若图片内没有明确指令，请描述和分析图片内容。"
        return "请读取并处理我从 Telegram 发送的附件。"

    telegram_failure_notices: dict[tuple[str, str], None] = {}

    async def notify_telegram_failure_once(
        event: dict[str, Any], chat_id: str, reason: str, message: str
    ) -> bool:
        """Send one failure notice per update/reason across bounded retries."""

        key = (str(event.get("update_id") or ""), reason)
        if key in telegram_failure_notices:
            return True
        try:
            await telegram_bridge.send_text(chat_id, message)
        except Exception as exc:
            log_telegram_delivery_failure(
                "Failed to report Telegram update failure", exc
            )
            return False
        if len(telegram_failure_notices) >= 1_024:
            telegram_failure_notices.pop(next(iter(telegram_failure_notices)))
        telegram_failure_notices[key] = None
        return True

    async def flush_pending_telegram_attachments(entry: dict) -> None:
        metadata = entry["metadata"]
        attachments = entry["attachments"]
        await launch_telegram_task(
            telegram_attachment_only_prompt(
                attachments, int(metadata.get("image_count") or 0)
            ),
            attachments,
            str(metadata["chat_id"]),
            acknowledge=False,
        )

    async def accept_telegram_update(event: dict[str, Any]) -> dict[str, Any]:
        if not telegram_bridge.configured:
            return {
                "ok": False,
                "reason": "telegram_not_configured",
                "ack_update": False,
            }
        prompt = str(event.get("prompt") or "").strip()
        resources = event.get("resources") or []
        chat_id = str(event.get("chat_id") or "").strip()
        if not chat_id or not (prompt or resources):
            return {"ok": True, "ignored": True, "ack_update": True}
        if not authorize_or_bind_telegram_chat(
            telegram_bridge,
            provider_secrets,
            chat_id,
            chat_type=str(event.get("chat_type") or ""),
        ):
            # Do not reply to an unbound sender: silence avoids confirming bot
            # activity while consuming the update so it cannot block the queue.
            return {
                "ok": True,
                "ignored": True,
                "reason": "telegram_chat_not_authorized",
                "ack_update": True,
            }
        attachments: list[str] = []
        voice_paths: list[str] = []
        image_count = 0
        voice_resources = [
            resource for resource in resources
            if str(resource.get("telegram_type") or "") == "voice"
        ]
        voice_acknowledged = False
        if voice_resources:
            try:
                await telegram_bridge.send_text(
                    chat_id,
                    "已收到你的语音要求，Elren 正在识别并准备执行。",
                )
                voice_acknowledged = True
            except Exception as exc:
                log_telegram_delivery_failure(
                    "Failed to acknowledge Telegram voice task", exc
                )
        if resources:
            update_id = re.sub(
                r"[^A-Za-z0-9_-]+", "_", str(event.get("update_id") or uuid4().hex)
            )
            destination = settings.screenshot_dir / "uploads" / "telegram" / update_id
            for resource in resources[:20]:
                try:
                    attachment = await telegram_bridge.download_file(
                        str(resource.get("file_id") or ""),
                        destination,
                        file_name=str(resource.get("file_name") or "telegram-file"),
                        max_bytes=20 * 1024 * 1024,
                    )
                except Exception as exc:
                    logger.warning(
                        "Telegram attachment processing failed for update %s: %s",
                        str(event.get("update_id") or "")[:40],
                        type(exc).__name__,
                    )
                    notified = await notify_telegram_failure_once(
                        event,
                        chat_id,
                        "attachment_download_failed",
                        "Elren 未能读取你发送的附件（attachment_download_failed）。"
                        "请确认文件可用且未超过 20 MB，然后重试。",
                    )
                    permanent_failure = isinstance(
                        exc, (ValueError, PermissionError)
                    )
                    return {
                        "ok": False,
                        "reason": "attachment_download_failed",
                        "ack_update": bool(notified and permanent_failure),
                    }
                attachments.append(str(attachment))
                if str(resource.get("telegram_type") or "") == "voice":
                    voice_paths.append(str(attachment))
            image_count = sum(
                str(resource.get("type") or "") == "image" for resource in resources
            )

        if voice_paths:
            transcripts: list[str] = []
            try:
                for voice_path in voice_paths:
                    transcripts.append(await speech_transcriber.transcribe(Path(voice_path)))
            except Exception as exc:
                logger.warning(
                    "Telegram voice transcription failed for update %s: %s",
                    str(event.get("update_id") or "")[:40],
                    type(exc).__name__,
                )
                notified = await notify_telegram_failure_once(
                    event,
                    chat_id,
                    "voice_transcription_failed",
                    "Elren 未能识别这条语音（voice_transcription_failed）。"
                    "请检查语音格式或本机语音设置，然后重试。",
                )
                return {
                    "ok": False,
                    "reason": "voice_transcription_failed",
                    # Retrying an unchanged local transcription failure every
                    # few seconds wastes resources and repeats the same user
                    # experience. Once the user is notified, consume it; if the
                    # notification itself failed, the bridge retries boundedly.
                    "ack_update": notified,
                }
            attachments = [item for item in attachments if item not in voice_paths]
            transcript_prompt = "\n".join(
                f"语音 {index} 转写：{text}" for index, text in enumerate(transcripts, 1)
            )
            prompt = "\n".join(value for value in (prompt, transcript_prompt) if value).strip()

        sender_id = str(event.get("sender_id") or "").strip()
        if not sender_id:
            # Never coalesce identity-less group messages across updates. They
            # flush independently instead of risking one sender consuming
            # another sender's attachment.
            message_scope = str(
                event.get("message_id") or event.get("update_id") or uuid4().hex
            ).strip()
            sender_id = f"message-{message_scope}"
        thread_id = str(event.get("message_thread_id") or "").strip() or "no-thread"
        conversation_key = f"telegram:{chat_id}:{thread_id}:{sender_id}"
        if attachments and not prompt:
            first_attachment = await pending_telegram_attachments.stage(
                conversation_key,
                attachments,
                {"chat_id": chat_id, "image_count": image_count},
                flush_pending_telegram_attachments,
            )
            if first_attachment:
                try:
                    await telegram_bridge.send_text(
                        chat_id,
                        "已收到你的远程要求。附件正在接收并等待后续说明；若 15 秒内没有补充，将自动开始处理。",
                    )
                except Exception as exc:
                    log_telegram_delivery_failure(
                        "Failed to acknowledge pending Telegram attachment", exc
                    )
            return {
                "ok": True,
                "pending_attachments": True,
                "attachment_count": len(attachments),
                "ack_update": True,
            }
        already_acknowledged = False
        if prompt:
            pending = pending_telegram_attachments.take(conversation_key)
            if pending:
                attachments = list(dict.fromkeys([*pending["attachments"], *attachments]))
                already_acknowledged = True
        return await launch_telegram_task(
            prompt or telegram_attachment_only_prompt(attachments, image_count),
            attachments,
            chat_id,
            acknowledge=not already_acknowledged and not voice_acknowledged,
            voice_request=bool(voice_paths),
        )

    @app.post("/api/tasks/{task_id}/retry-telegram-delivery")
    async def retry_telegram_delivery(task_id: str):
        task = await manager.get_async(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task.source != "telegram":
            raise HTTPException(409, "Only Telegram tasks can be redelivered")
        try:
            chat_id, _recipient_type = manager.remote_delivery_route(task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        manager.tasks.setdefault(task.id, task)
        delivered = await deliver_telegram_task_result(task, chat_id)
        if not delivered:
            raise HTTPException(502, telegram_bridge.last_error or "Telegram delivery failed")
        return {"ok": True, "task_id": task.id, "status": "delivered"}

    @app.post("/api/tasks/{task_id}/retry-feishu-delivery")
    async def retry_feishu_delivery(task_id: str):
        task = await manager.get_async(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task.source != "feishu":
            raise HTTPException(409, "Only Feishu tasks can be redelivered")
        try:
            receive_id, receive_id_type = manager.remote_delivery_route(task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        manager.tasks.setdefault(task.id, task)
        delivered = await deliver_feishu_task_result(
            task, receive_id, receive_id_type
        )
        if not delivered:
            raise HTTPException(502, feishu_bridge.last_error or "Feishu delivery failed")
        return {"ok": True, "task_id": task.id, "status": "delivered"}

    @app.post("/api/feishu/long-connection")
    async def feishu_long_connection(
        event: dict,
        x_elren_feishu_token: str = Header(default=""),
    ):
        if not feishu_bridge.validate_listener_token(x_elren_feishu_token):
            raise HTTPException(403, "飞书长连接工作进程校验失败")
        return await accept_feishu_event(event)

    @app.post("/api/feishu/events")
    async def feishu_events(request: Request):
        if not feishu_bridge.verification_token:
            raise HTTPException(410, "HTTP 回调已停用，请在飞书后台选择长连接接收事件")
        try:
            payload = await request.json()
            event = feishu_bridge.parse_event(payload, mark_seen=False)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if event and "challenge" in event:
            return event
        if not event:
            return {"ok": True, "ignored": True}
        return await accept_feishu_event(event)

    @app.post("/api/mcp/tools/{tool_name}")
    async def call_mcp_tool(tool_name: str, arguments: dict):
        if tool_name == "write_artifact":
            raise HTTPException(403, "write_artifact must pass through the Agent approval gate")
        try:
            return await mcp_runtime.call_tool(tool_name, arguments)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/openclaw/catalog")
    async def openclaw_catalog():
        try:
            return await openclaw_bridge.catalog()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/openclaw/plugins")
    async def openclaw_plugins():
        try:
            return await openclaw_bridge.plugins()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    upload_root = settings.screenshot_dir / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    max_upload_bytes = 25 * 1024 * 1024
    supported_upload_extensions = {
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff",
        ".pdf", ".doc", ".docx", ".xlsx", ".xls", ".csv", ".tsv", ".ppt", ".pptx",
        ".txt", ".md", ".json", ".jsonl", ".html", ".xml", ".yaml", ".yml",
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h",
        ".cs", ".go", ".rs", ".sql", ".ps1", ".sh",
        ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
        ".mp4", ".webm", ".mov", ".mkv",
    }

    def resolve_attachments(paths: list[str]) -> list[str]:
        resolved: list[str] = []
        for raw_path in paths:
            target = Path(raw_path).resolve()
            try:
                target.relative_to(upload_root.resolve())
            except ValueError as exc:
                raise HTTPException(422, "附件必须来自 Elren 上传区") from exc
            if not target.is_file() or target.suffix.lower() not in supported_upload_extensions:
                raise HTTPException(422, "附件不存在或格式不受当前技能链支持")
            resolved.append(str(target))
        return resolved

    def public_task_payload(
        task,
        *,
        event_slice: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Return UI task data without server-only routing capabilities."""

        if event_slice is None:
            payload = task.model_dump()
        else:
            # Running-task polling can request an inclusive event suffix. Avoid
            # serializing the full bounded history only to discard it again.
            payload = task.model_dump(exclude={"events", "conversation_turns"})
            payload["events"] = [event.model_dump() for event in event_slice]
        # The browser never reads or writes remote delivery routes. Exposing
        # them in task JSON would let WebView2 duplicate recipient identifiers
        # in its persistent HTTP cache.
        for private_field in (
            "remote_recipient_id",
            "remote_recipient_type",
        ):
            payload.pop(private_field, None)
        # Discussion-team private instructions are server control data, not UI
        # content.  The new specialist snapshots intentionally contain no such
        # field, while legacy team records need this explicit compatibility scrub.
        for member in payload.get("discussion_team") or []:
            if isinstance(member, dict):
                member.pop("system_prompt", None)
        return add_task_display_overrides(payload)

    @app.get("/api/uploads/{upload_path:path}")
    async def get_uploaded_file(upload_path: str):
        target = (upload_root / upload_path).resolve()
        try:
            target.relative_to(upload_root.resolve())
        except ValueError as exc:
            raise HTTPException(403, "Upload path escapes the attachment directory") from exc
        if not target.is_file():
            raise HTTPException(404, "Uploaded file not found")
        return FileResponse(target)

    @app.post("/api/uploads")
    async def upload_file(
        request: Request,
        x_filename: str = Header(default="", alias="X-Filename"),
        x_content_type: str = Header(default="", alias="X-Content-Type"),
    ):
        chinese = request.headers.get("accept-language", "en").lower().startswith("zh")

        def reject_upload(status: int, code: str, zh: str, en: str, **extra):
            return HTTPException(status, {"code": code, "message": zh if chinese else en, **extra})

        original_name = Path(unquote(x_filename).replace("\x00", "")).name
        if not original_name:
            raise reject_upload(422, "upload_missing_filename", "缺少文件名", "A filename is required")
        extension = Path(original_name).suffix.lower()
        if extension not in supported_upload_extensions:
            raise reject_upload(415, "upload_unsupported_format", "不支持此附件格式", "This attachment format is not supported")
        media_extensions = {
            ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
            ".mp4", ".webm", ".mov", ".mkv",
        }
        upload_limit = 100 * 1024 * 1024 if extension in media_extensions else max_upload_bytes
        limit_mb = upload_limit // (1024 * 1024)

        def too_large():
            return reject_upload(
                413, "upload_too_large", f"单个附件不能超过 {limit_mb} MB",
                f"Each attachment must be {limit_mb} MB or smaller", limit_bytes=upload_limit,
            )

        try:
            declared_size = max(int(request.headers.get("content-length") or 0), 0)
        except ValueError as exc:
            raise reject_upload(400, "upload_invalid_length", "附件长度信息无效", "Invalid Content-Length header") from exc
        if declared_size > upload_limit:
            raise too_large()
        # Do not trust Content-Length: chunked clients can omit or falsify it.
        # Keep the existing in-memory upload contract, but stop reading as soon
        # as the per-type limit is exceeded so one request cannot consume
        # unbounded service memory.
        body_buffer = bytearray()
        async for chunk in request.stream():
            if len(body_buffer) + len(chunk) > upload_limit:
                raise too_large()
            body_buffer.extend(chunk)
        body = body_buffer
        if not body:
            raise reject_upload(422, "upload_empty_file", "文件为空", "The selected file is empty")
        if len(body) > upload_limit:
            raise too_large()
        safe_stem = re.sub(r"[^\w.()\- ]+", "_", Path(original_name).stem)[:100] or "file"
        target_dir = upload_root / uuid4().hex
        target = target_dir / f"{safe_stem}{extension}"
        try:
            # Slow disks, antivirus replacement retries and fsync must not
            # block chat/status requests on the service event loop. Retain
            # ownership until the write settles even if the client disconnects.
            await run_owned_thread(save_upload, target, body)
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EDQUOT} or getattr(exc, "winerror", None) in {
                39, 112, 1295,
            }:
                raise reject_upload(
                    507, "upload_storage_full", "存储空间不足，附件未保存，请释放空间后重试",
                    "Not enough storage space. The attachment was not saved; free space and retry.",
                ) from exc
            if isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
                raise reject_upload(
                    503, "upload_storage_denied",
                    "无法写入附件目录，请检查文件夹权限或占用情况后重试",
                    "Cannot write to the attachment folder. Check folder permissions or file locks and retry.",
                ) from exc
            raise reject_upload(
                503, "upload_storage_failed", "附件保存失败，请检查存储设备后重试",
                "The attachment could not be saved. Check the storage device and retry.",
            ) from exc
        return {
            "path": str(target.resolve()),
            "name": original_name,
            "size": len(body),
            "content_type": x_content_type,
            "url": "/api/uploads/" + "/".join(
                quote(part, safe="") for part in target.relative_to(upload_root).parts
            ),
            "kind": (
                "image" if extension in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
                else "audio" if extension in {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
                else "video" if extension in {".mp4", ".webm", ".mov", ".mkv"}
                else "document"
            ),
        }

    def selected_project(request: CreateTaskRequest | ProjectPathRequest, http_request: Request) -> str:
        from deepdesk.task_projects import is_loopback_client, normalize_project_path

        if request.project_path and request.project_path.strip() and (
            not is_loopback_client(http_request) or not same_origin_browser_request(http_request)
        ):
            raise HTTPException(403, "Project selection requires the local same-origin app page")
        try:
            return normalize_project_path(request.project_path)
        except ValueError as exc:
            raise HTTPException(422, {"code": "invalid_project_path", "message": str(exc)}) from exc

    @app.post("/api/projects/validate")
    async def validate_project(http_request: Request, body: ProjectPathRequest):
        project_path = selected_project(body, http_request)
        return {"project_path": project_path, "name": Path(project_path).name if project_path else ""}

    @app.post("/api/tasks")
    async def create_task(request: CreateTaskRequest, http_request: Request):
        project_path = selected_project(request, http_request)
        try:
            team_selected, model_preference = resolve_task_model_preference(
                request.model_preference,
                runtime_store.value,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not client.has_model_credentials(model_preference):
            raise HTTPException(
                409,
                {
                    "code": "model_api_key_required",
                    "message": "尚未配置可用于所选模型的 API 密钥，请先在“设置 → 模型与密钥”中配置。",
                },
            )
        if team_selected:
            unavailable_team_models = [
                member.model
                for member in runtime_store.value.discussion_team
                if member.model != "auto" and not client.has_model_credentials(member.model)
            ]
            if unavailable_team_models:
                raise HTTPException(
                    409,
                    {
                        "code": "team_model_api_key_required",
                        "message": "讨论团成员模型当前不可用，请在“设置 → 讨论团”中重新选择："
                        + "、".join(unavailable_team_models),
                    },
                )
        task = await manager.create_async(
            request.prompt.strip(),
            ApprovalPolicy.AUTONOMOUS,
            AgentProfile.GENERAL,
            project_path=project_path,
            model_preference=model_preference,
            reasoning_effort=(
                runtime_store.value.reasoning_effort
                if request.reasoning_effort == "default"
                else request.reasoning_effort
            ),
            active_model=(
                model_preference
                if model_preference != "auto"
                else client.model
            ),
            attachments=resolve_attachments(request.attachments),
            interface_language=request.interface_language,
            voice_request=request.voice_request,
            **discussion_team_snapshot(enabled=team_selected),
        )
        return public_task_payload(task)

    @app.post("/api/tasks/{task_id}/continue")
    async def continue_task(task_id: str, request: CreateTaskRequest, http_request: Request):
        previous = await manager.get_async(task_id)
        if not previous:
            raise HTTPException(404, "Task not found")
        if request.project_path is not None and selected_project(request, http_request) != previous.project_path:
            raise HTTPException(409, "A conversation keeps its original project; create a new task to switch projects")
        try:
            team_selected, model_preference = resolve_task_model_preference(
                request.model_preference,
                runtime_store.value,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not client.has_model_credentials(model_preference):
            raise HTTPException(
                409,
                {
                    "code": "model_api_key_required",
                    "message": "尚未配置可用于所选模型的 API 密钥，请先在“设置 → 模型与密钥”中配置。",
                },
            )
        if team_selected:
            unavailable_team_models = [
                member.model
                for member in runtime_store.value.discussion_team
                if member.model != "auto" and not client.has_model_credentials(member.model)
            ]
            if unavailable_team_models:
                raise HTTPException(
                    409,
                    {
                        "code": "team_model_api_key_required",
                        "message": "讨论团成员模型当前不可用，请在“设置 → 讨论团”中重新选择："
                        + "、".join(unavailable_team_models),
                    },
                )
        manager.tasks.setdefault(previous.id, previous)
        try:
            # A terminal status can be visible before the worker's finally
            # block finishes. Never overlap two executions of the same ID.
            worker = manager.background.get(task_id)
            if previous.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED} and worker is not None and not worker.done():
                try:
                    await asyncio.wait_for(wait_owned_result(worker), timeout=2)
                except TimeoutError as exc:
                    raise ValueError("上一轮仍在收尾，请稍后重试") from exc
            task = await manager.continue_from_async(
                previous,
                request.prompt.strip(),
                ApprovalPolicy.AUTONOMOUS,
                AgentProfile.GENERAL,
                same_conversation=True,
                model_preference=model_preference,
                reasoning_effort=(
                    runtime_store.value.reasoning_effort
                    if request.reasoning_effort == "default"
                    else request.reasoning_effort
                ),
                attachments=resolve_attachments(request.attachments),
                interface_language=request.interface_language,
                voice_request=request.voice_request,
                **discussion_team_snapshot(enabled=team_selected),
            )
            return public_task_payload(task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/tasks/{task_id}/messages")
    async def send_running_task_message(
        task_id: str, request: RunningTaskMessageRequest
    ):
        task = await manager.get_async(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        manager.tasks.setdefault(task.id, task)
        try:
            task = await manager.steer_async(
                task.id,
                request.prompt.strip(),
                resolve_attachments(request.attachments),
            )
            return public_task_payload(task)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    outputs_dir = settings.workspace / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    artifact_index_cache: dict[str, dict[str, Any]] = {}
    artifact_index_updated_at = 0.0
    artifact_refresh_task: asyncio.Task[Any] | None = None
    artifact_internal_names = frozenset(
        {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".cache"}
    )

    def is_internal_artifact_name(name: str) -> bool:
        return name.casefold() in artifact_internal_names

    def scan_artifact_index() -> dict[str, dict[str, Any]]:
        """Build folder summaries without blocking the HTTP event loop."""
        snapshot: dict[str, dict[str, Any]] = {}
        for path in outputs_dir.iterdir():
            if is_internal_artifact_name(path.name):
                continue
            try:
                is_link = path.is_symlink()
                stat = path.lstat() if is_link else path.stat()
            except OSError:
                continue
            if is_link:
                snapshot[path.name] = {
                    "path": path.name,
                    "name": path.name,
                    "kind": "link",
                    "size": 0,
                    "file_count": 0,
                    "modified": stat.st_mtime,
                    "url": None,
                }
                continue
            if path.is_file():
                snapshot[path.name] = {
                    "path": path.name,
                    "name": path.name,
                    "kind": "file",
                    "size": stat.st_size,
                    "file_count": 1,
                    "modified": stat.st_mtime,
                    "url": f"/api/artifacts/{quote(path.name, safe='')}",
                }
                continue
            size = 0
            file_count = 0
            modified = stat.st_mtime
            for root, directories, filenames in os.walk(path, followlinks=False):
                root_path = Path(root)
                directories[:] = [
                    name
                    for name in directories
                    if not is_internal_artifact_name(name)
                    and not (root_path / name).is_symlink()
                ]
                for filename in filenames:
                    if is_internal_artifact_name(filename):
                        continue
                    target = root_path / filename
                    try:
                        child_stat = target.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    size += child_stat.st_size
                    file_count += 1
                    modified = max(modified, child_stat.st_mtime)
            snapshot[path.name] = {
                "path": path.name,
                "name": path.name,
                "kind": "folder",
                "size": size,
                "file_count": file_count,
                "modified": modified,
                "url": None,
            }
        return snapshot

    async def refresh_artifact_index() -> None:
        nonlocal artifact_index_cache, artifact_index_updated_at
        nonlocal artifact_refresh_task
        try:
            artifact_index_cache = await asyncio.to_thread(scan_artifact_index)
            artifact_index_updated_at = time.monotonic()
        finally:
            artifact_refresh_task = None

    @app.get("/api/artifacts")
    async def list_artifacts():
        nonlocal artifact_refresh_task
        cache_stale = time.monotonic() - artifact_index_updated_at > 15
        if artifact_refresh_task is None and (
            artifact_index_updated_at == 0 or cache_stale
        ):
            artifact_refresh_task = spawn_auxiliary(
                refresh_artifact_index(), name="artifact-index-refresh"
            )
        artifacts: list[dict[str, Any]] = []
        for path in sorted(outputs_dir.iterdir(), key=lambda item: item.name.casefold()):
            if is_internal_artifact_name(path.name):
                continue
            cached = artifact_index_cache.get(path.name)
            if cached is not None:
                artifacts.append(cached)
                continue
            try:
                is_link = path.is_symlink()
                stat = path.lstat() if is_link else path.stat()
            except OSError:
                continue
            is_file = path.is_file() and not is_link
            artifacts.append(
                {
                    "path": path.name,
                    "name": path.name,
                    "kind": "file" if is_file else ("link" if is_link else "folder"),
                    "size": stat.st_size if is_file else 0,
                    "file_count": 1 if is_file else 0,
                    "modified": stat.st_mtime,
                    "url": f"/api/artifacts/{quote(path.name, safe='')}" if is_file else None,
                    "summary_pending": not is_file,
                }
            )
        artifacts.sort(
            key=lambda artifact: (
                -float(artifact.get("modified") or 0),
                str(artifact.get("name") or "").casefold(),
            )
        )
        return {
            "artifacts": artifacts,
            "count": len(artifacts),
            "root": str(outputs_dir),
            "summary_pending": artifact_refresh_task is not None,
        }

    @app.get("/api/artifacts/{artifact_path:path}")
    async def get_artifact(artifact_path: str):
        target = (outputs_dir / artifact_path).resolve()
        try:
            target.relative_to(outputs_dir.resolve())
        except ValueError as exc:
            raise HTTPException(403, "Artifact path escapes outputs") from exc
        if not target.is_file():
            raise HTTPException(404, "Artifact not found")
        return FileResponse(target, filename=target.name)

    @app.get("/api/schedules")
    async def list_schedules():
        return await scheduler.run_async(
            lambda: {"schedules": scheduler.list(), "stats": scheduler.stats()},
        )

    @app.post("/api/schedules")
    async def create_schedule(request: ScheduleRequest):
        try:
            return await scheduler.run_async(scheduler.create, request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/schedules/{schedule_id}")
    async def update_schedule(schedule_id: str, request: SchedulePatch):
        try:
            result = await scheduler.run_async(scheduler.update, schedule_id, request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not result:
            raise HTTPException(404, "Schedule not found")
        return result

    @app.get("/api/desktop/identity")
    async def desktop_identity():
        """Identify the package that owns the loopback desktop backend.

        The native shell uses only this non-secret digest to avoid attaching to
        a stale Elren process started from another extracted package.
        """
        normalized_root = str(settings.workspace).rstrip("\\/").lower()
        root_hash = hashlib.sha256(normalized_root.encode("utf-8")).hexdigest()
        return {"app": "Elren", "version": "1.0.0", "root_hash": root_hash}

    @app.put("/api/desktop/language")
    async def desktop_language(
        request: Request,
        language: Literal["zh", "en"] = Body(..., embed=True),
    ):
        """Bridge the external-browser fallback's language to the native tray."""

        if not same_origin_browser_request(request):
            raise HTTPException(403, "Desktop language updates require the same-origin app page")
        try:
            # This intentionally does not touch runtime settings or credentials.
            # The shell accepts exactly the same two-byte language preference.
            (settings.data_dir / "desktop-ui-language.txt").write_text(
                language, encoding="utf-8"
            )
        except OSError:
            raise HTTPException(503, "Desktop language preference could not be saved") from None
        return {"ok": True, "language": language}

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str):
        if not await scheduler.run_async(scheduler.delete, schedule_id):
            raise HTTPException(404, "Schedule not found")
        return {"ok": True}

    @app.post("/api/schedules/{schedule_id}/run")
    async def run_schedule(schedule_id: str):
        try:
            result = await scheduler.run_async(scheduler.run_now, schedule_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not result:
            raise HTTPException(404, "Schedule not found")
        return result

    @app.get("/api/tasks")
    async def list_tasks(
        limit: int = 50,
        offset: int = 0,
        query: str = "",
        status: str = "",
    ):
        # SQLite reads, persistence safety checks and full task deserialization
        # can be expensive for long histories. Keep them off the ASGI event
        # loop so status/settings/identity requests remain responsive.
        tasks, total = await asyncio.to_thread(
            task_store.list_page,
            limit=limit,
            offset=offset,
            query=query,
            status=status,
        )
        page_limit = min(max(limit, 1), 100)
        page_offset = max(offset, 0)
        return {
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "pinned": task.pinned,
                    "prompt": task.prompt,
                    "status": task.status,
                    "agent_profile": task.agent_profile,
                    "active_model": task.active_model,
                    "source": task.source,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                }
                for task in tasks
            ],
            "total": total,
            "offset": page_offset,
            "limit": page_limit,
            "has_more": page_offset + len(tasks) < total,
        }

    @app.patch("/api/tasks/{task_id}/pin")
    async def pin_task(task_id: str, pinned: bool = Body(..., embed=True, strict=True)):
        try:
            found = await asyncio.to_thread(task_store.set_pinned, task_id, pinned)
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(503, "Could not save chat pin; please retry") from exc
        if not found:
            raise HTTPException(404, "Task not found")
        return {"ok": True, "task_id": task_id, "pinned": pinned}

    @app.get("/api/tasks/{task_id}/file")
    async def download_task_file(task_id: str, path: str):
        from .task_file_links import referenced_task_file
        task = await asyncio.to_thread(task_store.get, task_id)
        if task is None:
            raise HTTPException(404, "Task not found")
        try:
            target = await asyncio.to_thread(referenced_task_file, task, settings.workspace, path)
        except PermissionError:
            raise HTTPException(403, "File is not an authorized task output") from None
        except (FileNotFoundError, OSError, ValueError):
            raise HTTPException(404, "Output file no longer exists") from None
        return FileResponse(target, filename=target.name, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        })

    @app.get("/api/tasks/{task_id}/export")
    async def download_conversation(task_id: str, format: Literal["markdown", "json"] = "markdown"):
        # Use the sanitized durable snapshot, never the live unredacted request.
        task = await asyncio.to_thread(task_store.get, task_id)
        if task is None:
            raise HTTPException(404, "Task not found")
        content = await asyncio.to_thread(export_conversation, task, format)
        extension = "json" if format == "json" else "md"
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", task.id)[:64] or "chat"
        return Response(content, media_type="application/json" if format == "json" else "text/markdown",
                        headers={"Content-Disposition": f'attachment; filename="elren-{safe_id}.{extension}"',
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, after_event_id: str = ""):
        task = await manager.get_async(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        manager.tasks.setdefault(task.id, task)
        pending = [a.model_dump() for a in approvals.pending.values() if a.task_id == task_id]
        pending_human = [
            request.model_dump()
            for request in human_actions.pending.values()
            if request.task_id == task_id
        ]
        cursor = str(after_event_id or "")[:64]
        event_slice = None
        if cursor:
            # Include the cursor event itself so the browser can replace a
            # coalesced/progress event in place. If a bounded history evicted
            # the cursor, fall back to a complete snapshot for correctness.
            for index in range(len(task.events) - 1, -1, -1):
                if task.events[index].id == cursor:
                    event_slice = task.events[index:]
                    break
        payload = public_task_payload(task, event_slice=event_slice)
        if event_slice is not None:
            payload["event_delta"] = True
            payload["event_delta_includes_cursor"] = True
        return {
            **payload,
            "pending_approvals": pending,
            "pending_human_actions": pending_human,
        }

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        if task_id not in manager.tasks:
            raise HTTPException(404, "Task not found")
        accepted = manager.cancel(task_id)
        return {
            "ok": accepted,
            "status": "cancelling" if accepted else manager.tasks[task_id].status,
            "message": "停止请求已发送" if accepted else "任务已经结束",
        }

    @app.patch("/api/tasks/{task_id}/title")
    async def rename_task(
        task_id: str,
        request: TaskTitlePatch,
        http_request: Request,
    ):
        task = await manager.get_async(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        title = re.sub(r"\s+", " ", request.title).strip(" -—:：")
        if not title:
            raise HTTPException(422, "Conversation title cannot be empty")
        try:
            task = await manager.rename_async(task_id, title[:60], "user")
            if task is None:
                raise HTTPException(404, "Task not found")
        except (OSError, sqlite3.Error) as exc:
            chinese = http_request.headers.get("accept-language", "en").lower().startswith("zh")
            raise HTTPException(503, {
                "code": "task_title_save_failed",
                "message": (
                    "标题保存失败，原有标题已保留，请检查存储后重试。" if chinese else
                    "The title could not be saved. The original title was kept; check storage and retry."
                ),
            }) from exc
        return {"ok": True, "task_id": task.id, "title": task.title}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        task = await manager.get_async(task_id)
        if not task:
            raise HTTPException(404, "Task not found")

        active = task.status in {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.WAITING,
            TaskStatus.WAITING_USER,
        }
        if active:
            manager.tasks.setdefault(task.id, task)
            manager.cancel(task_id)
            background = manager.background.get(task_id)
            if background and not background.done():
                try:
                    await asyncio.wait_for(wait_owned_result(background), timeout=3)
                except TimeoutError:
                    # Deletion should not hang on a plugin that is slow to unwind.
                    pass
                except asyncio.CancelledError:
                    # Waiting also raises when the HTTP owner is cancelled. Only
                    # a worker's cancellation is an expected shutdown outcome;
                    # do not continue deleting after cancellation of this request.
                    owner = asyncio.current_task()
                    if owner is not None and owner.cancelling():
                        raise
                except Exception as exc:
                    # A worker may fail while persisting its cancellation. The
                    # requested DELETE is independent and may still succeed.
                    # Keep actual delete errors visible and never log raw worker
                    # exception bodies (provider errors can contain credentials).
                    logger.warning("Task %s cleanup failed before deletion (%s)",
                                   task_id, type(exc).__name__)

        deleted = await manager.delete_async(task_id)
        # A cancellation-resistant extension may still be unwinding after the
        # bounded HTTP wait.  Keep that worker registered so app shutdown can
        # observe/cancel it again; TaskStore's deletion tombstone prevents any
        # late event from resurrecting the removed conversation.
        return {"ok": True, "deleted": deleted, "task_id": task_id}

    @app.post("/api/approvals/{approval_id}")
    async def decide_approval(approval_id: str, decision: ApprovalDecision):
        if not approvals.resolve(approval_id, decision.approved):
            raise HTTPException(404, "Approval is no longer pending")
        return {"ok": True}

    @app.post("/api/human-actions/{request_id}/takeover")
    async def take_over_human_action(request_id: str):
        request = human_actions.take_over(request_id)
        if not request:
            raise HTTPException(404, "Human action is no longer pending")
        task = manager.tasks.get(request.task_id)
        if task:
            await manager.emit_async(
                task,
                "human_takeover",
                {"request_id": request.id, "target_window": request.target_window},
            )
        focus_result = await focus_window(request.target_window)
        return {"ok": True, "request": request.model_dump(), "focus": focus_result}

    @app.post("/api/human-actions/{request_id}/complete")
    async def complete_human_action(request_id: str, decision: HumanActionDecision):
        request = human_actions.pending.get(request_id)
        if not request:
            raise HTTPException(404, "Human action is no longer pending")
        if not request.taken_over:
            raise HTTPException(409, "请先点击接管操作")
        if not human_actions.resolve(
            request_id,
            decision.completed,
            decision.issue_description,
            decision.skipped_description,
        ):
            raise HTTPException(409, "Human action could not be resumed")
        return {
            "ok": True,
            "completed": decision.completed,
            "issue_description": decision.issue_description,
        }

    static_dir = Path(__file__).parent / "static"
    app.mount("/screenshots", StaticFiles(directory=settings.screenshot_dir), name="screenshots")

    @app.get("/")
    async def index():
        return FileResponse(static_dir / "index.html")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


def run() -> None:
    # Importing this module (including pytest collection) must never create or
    # apply a persistent workspace baseline. Only the explicit desktop/service
    # entry point crosses that durable trust boundary.
    service_app = create_app(persistent_control_baseline=True)
    settings = service_app.state.settings
    url = f"http://{settings.deepdesk_host}:{settings.deepdesk_port}"
    skip_browser = os.getenv(
        "ELREN_SKIP_AUTO_BROWSER",
        os.getenv("MILO_SKIP_AUTO_BROWSER", os.getenv("DEEPDESK_SKIP_AUTO_BROWSER", "")),
    )
    if skip_browser.strip() != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # Uvicorn writes normal INFO messages to stderr by default. Windows
    # PowerShell 5 wraps every native stderr line as ``NativeCommandError``,
    # which made a healthy launch log look like a failure. Keep real logging,
    # but send both standard Uvicorn handlers to stdout for clean launcher logs.
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["handlers"]["default"]["stream"] = "ext://sys.stdout"
    log_config["handlers"]["access"]["stream"] = "ext://sys.stdout"
    config = uvicorn.Config(
        service_app,
        host=settings.deepdesk_host,
        port=settings.deepdesk_port,
        timeout_graceful_shutdown=5.0,
        log_level="info",
        log_config=log_config,
        # Browser polling is intentionally frequent while an Agent runs. The
        # product audit log already records meaningful actions; duplicating
        # every local GET in the launcher log created hundreds of kilobytes of
        # noise per session and measurable disk churn on slower computers.
        access_log=os.getenv(
            "ELREN_ACCESS_LOG",
            os.getenv("MILO_ACCESS_LOG", os.getenv("DEEPDESK_ACCESS_LOG", "")),
        ).strip()
        == "1",
    )
    from .desktop_lifecycle import serve_desktop, service_instance

    asyncio.run(serve_desktop(
        uvicorn.Server(config), Path(__file__).resolve().parents[1] / "data",
        service_instance(sys.argv[1:]),
    ))


if __name__ == "__main__":
    run()
