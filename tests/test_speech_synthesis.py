import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from deepdesk.config import Settings
from deepdesk.main import create_app
from deepdesk.speech_synthesis import SpeechAudio, SpeechSynthesizer
from deepdesk.speech_transcription import (
    SUPPORTED_SPEECH_LANGUAGES,
    _normalize_speech_language,
    system_speech_language,
)


def _javascript_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Could not extract JavaScript function {name}")


def test_reply_speech_normalizes_markdown_and_selects_language(tmp_path: Path):
    speech = SpeechSynthesizer(tmp_path)

    assert speech._plain_text("## 标题\n**你好** [链接](https://example.com)") == "标题 你好 链接"
    assert speech._language("这是一段中文回复", "auto") == "zh-CN"
    assert speech._language("This is an English reply", "auto") == "en-US"
    assert speech._language("これは日本語です", "auto") == "ja-JP"
    assert speech._language("Привет, мир", "auto") == "ru-RU"
    assert speech._voice("zh-CN", "") == "zh-CN-XiaoxiaoNeural"
    assert speech._voice("ja-JP", "") == "ja-JP-NanamiNeural"


def test_speech_module_does_not_import_edge_tts_during_web_startup():
    root = Path(__file__).parents[1]
    code = (
        "import sys; import deepdesk.speech_synthesis; "
        "raise SystemExit(1 if 'edge_tts' in sys.modules else 0)"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=10,
    )


def test_system_dictation_language_is_independent_of_interface_language():
    assert system_speech_language() in SUPPORTED_SPEECH_LANGUAGES
    assert _normalize_speech_language("zh_CN") == "zh-CN"
    assert _normalize_speech_language("fr") == "fr-FR"
    assert len(SUPPORTED_SPEECH_LANGUAGES) >= 20


def test_live_voice_draft_replaces_interim_text_and_respects_manual_scroll():
    root = Path(__file__).parents[1]
    source = (root / "deepdesk/static/voice.js").read_text("utf-8")
    node = root / "work/node-runtime/node.exe"
    script = f"""
let liveInterimText = "";
let draftAutoFollow = true;
const DRAFT_BOTTOM_THRESHOLD = 18;
const draft = {{value: "", scrollTop: 0, scrollHeight: 500, clientHeight: 100}};
const $v = () => draft;
{_javascript_function(source, "draftIsNearBottom")}
{_javascript_function(source, "updateLiveDraft")}
updateLiveDraft("", "temporary words");
const interim = draft.value;
updateLiveDraft("final words", "next words");
const replacement = draft.value;
draft.value = "earlier ".repeat(80);
liveInterimText = "";
draft.scrollTop = 17;
draftAutoFollow = false;
updateLiveDraft("", "new speech");
const pausedScroll = draft.scrollTop;
draft.scrollTop = 400;
draftAutoFollow = draftIsNearBottom();
updateLiveDraft("", "continued");
process.stdout.write(JSON.stringify({{interim, replacement, pausedScroll, followedScroll: draft.scrollTop}}));
"""
    result = subprocess.run(
        [str(node), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    state = json.loads(result.stdout)
    assert state == {
        "interim": "temporary words",
        "replacement": "final words next words",
        "pausedScroll": 17,
        "followedScroll": 500,
    }


@pytest.mark.asyncio
async def test_reply_speech_uses_neural_audio_and_cache(tmp_path: Path, monkeypatch):
    calls = []

    class Communicate:
        def __init__(self, text, voice, rate):
            calls.append((text, voice, rate))

        async def save(self, path):
            Path(path).write_bytes(b"ID3" + b"audio" * 200)

    monkeypatch.setattr(
        "deepdesk.speech_synthesis.edge_tts",
        SimpleNamespace(Communicate=Communicate),
    )
    speech = SpeechSynthesizer(tmp_path)
    first = await speech.synthesize("你好，欢迎使用语音回复。", language="zh-CN", rate=1.15)
    second = await speech.synthesize("你好，欢迎使用语音回复。", language="zh-CN", rate=1.15)

    assert first == second
    assert first.media_type == "audio/mpeg"
    assert first.provider == "edge-neural"
    assert first.path.stat().st_size > 512
    assert calls == [("你好，欢迎使用语音回复。", "zh-CN-XiaoxiaoNeural", "+15%")]


@pytest.mark.asyncio
async def test_reply_speech_streams_first_audio_and_builds_cache(tmp_path: Path, monkeypatch):
    calls = []

    class Communicate:
        def __init__(self, text, voice, rate):
            calls.append((text, voice, rate))

        async def stream(self):
            yield {"type": "metadata", "data": b""}
            yield {"type": "audio", "data": b"ID3" + b"a" * 400}
            yield {"type": "audio", "data": b"b" * 400}

    monkeypatch.setattr(
        "deepdesk.speech_synthesis.edge_tts",
        SimpleNamespace(Communicate=Communicate),
    )
    speech = SpeechSynthesizer(tmp_path)
    first = b"".join(
        [
            chunk
            async for chunk in speech.stream_mp3(
                "现在开始播放。", language="zh-CN", rate=1.0
            )
        ]
    )
    second = b"".join(
        [
            chunk
            async for chunk in speech.stream_mp3(
                "现在开始播放。", language="zh-CN", rate=1.0
            )
        ]
    )

    assert first == second
    assert first.startswith(b"ID3")
    assert len(first) > 512
    assert calls == [("现在开始播放。", "zh-CN-XiaoxiaoNeural", "+0%")]


def test_speech_api_returns_playable_audio(tmp_path: Path, monkeypatch):
    audio_path = tmp_path / "reply.mp3"
    audio_path.write_bytes(b"ID3" + b"audio" * 200)

    async def synthesize(self, text, **_kwargs):
        assert text == "请朗读这段回复"
        return SpeechAudio(audio_path, "audio/mpeg", "test-neural", "test-voice")

    monkeypatch.setattr(SpeechSynthesizer, "synthesize", synthesize)
    settings = Settings(
        _env_file=None,
        deepdesk_workspace=tmp_path,
        deepdesk_openclaw_enabled=False,
        deepdesk_feishu_enabled=False,
        deepseek_api_key="",
        deepseek_backup_api_key="",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/speech/synthesize",
            json={"text": "请朗读这段回复", "language": "zh-CN", "rate": 1.0},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.headers["x-elren-speech-provider"] == "test-neural"
    assert response.content == audio_path.read_bytes()


def test_speech_stream_session_is_progressive_and_one_shot(tmp_path: Path, monkeypatch):
    async def stream_mp3(self, text, **_kwargs):
        assert text == "立即播放这条回复"
        yield b"ID3"
        yield b"audio" * 200

    monkeypatch.setattr(SpeechSynthesizer, "stream_mp3", stream_mp3)
    monkeypatch.setattr(
        SpeechSynthesizer,
        "status",
        lambda self: {
            "ready": True,
            "online_neural": True,
            "windows_fallback": True,
            "voices": [],
        },
    )
    settings = Settings(
        _env_file=None,
        deepdesk_workspace=tmp_path,
        deepdesk_openclaw_enabled=False,
        deepdesk_feishu_enabled=False,
        deepseek_api_key="",
        deepseek_backup_api_key="",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        session = client.post(
            "/api/speech/stream-sessions",
            json={"text": "立即播放这条回复", "language": "zh-CN", "rate": 1.0},
        )
        assert session.status_code == 200
        url = session.json()["stream_url"]
        first = client.get(url)
        expired = client.get(url)

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("audio/mpeg")
    assert first.headers["x-elren-speech-provider"] == "edge-neural-stream"
    assert first.content.startswith(b"ID3")
    assert expired.status_code == 404


def test_frontend_speaks_normal_chat_and_has_server_fallback():
    root = Path(__file__).parents[1]
    voice = (root / "deepdesk/static/voice.js").read_text("utf-8")
    voice_css = (root / "deepdesk/static/voice.css").read_text("utf-8")
    index = (root / "deepdesk/static/index.html").read_text("utf-8")
    launcher = (root / "launcher/ElrenLauncher.cs").read_text("utf-8")

    assert 'fetch("/api/speech/synthesize"' in voice
    assert 'fetch("/api/speech/stream-sessions"' in voice
    assert 'new Audio(session.stream_url)' in voice
    assert "speechEligibleTaskIds" not in voice
    assert 'if (!dialogOpen || (!awaitingTask && !activeVoiceTaskId)) return;' in voice
    assert 'if (!$v("#voiceDialog")?.open || task?.status !== "completed"' in voice
    assert "task?.display_result ?? task?.result" in voice
    assert 'appendMessage("assistant", displayResult)' in voice
    assert "speechChunks(displayResult)" in voice
    assert "speechQueue.unshift(chunk)" in voice
    assert 'typeof SpeechSynthesisUtterance !== "function"' in voice
    assert "/static/voice.js?v=18" in index
    assert "await start({ voiceRequest: true });" in voice
    assert "const AUTO_SEND_SILENCE_MS = 2400" in voice
    assert "scheduleAutoSend();" in voice
    assert "setTimeout(submitTurn, 700)" not in voice
    assert 'window.addEventListener("elren:task-view-changed"' in voice
    assert "transcript.replaceChildren();" in voice
    assert "voiceMarkdownPreview" not in index
    assert "voiceInterim" not in index
    assert "updateMarkdownPreview" not in voice
    assert "updateLiveDraft(finalText, interimText)" in voice
    assert "draftAutoFollow = draftIsNearBottom()" in voice
    assert "draft.scrollTop = savedScrollTop" in voice
    assert 'body.className = "markdown"' in voice
    assert ".voice-draft-shell{width:100%}" in voice_css
    assert ".voice-composer textarea{display:block;width:100%;height:160px" in voice_css
    assert ".voice-composer.markdown-active" not in voice_css
    assert "preferences.system_voice_language" in voice
    assert '|| navigator.languages?.[0]' in voice
    assert 'isEnglish() ? "en-US" : "zh-CN"' not in voice
    assert 'data-recognition-language' in voice
    app = (root / "deepdesk/static/app.js").read_text("utf-8")
    assert '$("#voiceDialog")?.open ? 200 : 650' in app
    assert "ja-JP-NanamiNeural" in app
    assert "ar-SA-ZariyahNeural" in app
    assert "uk-UA-PolinaNeural" in app
    assert "--autoplay-policy=no-user-gesture-required" in launcher
