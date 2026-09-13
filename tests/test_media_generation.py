from pathlib import Path

import pytest

from deepdesk.media_generation import FreeMediaGenerator
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.media_generation import MediaGenerationTool


def test_free_model_filter_blocks_paid_only_and_selects_each_modality(tmp_path: Path):
    generator = FreeMediaGenerator("test-key", tmp_path)

    assert generator._is_free_tier_model(
        {"name": "image-free", "category": "image", "output_modalities": ["image"], "pricing": {"request": 0}},
        "image",
    )
    assert not generator._is_free_tier_model(
        {"name": "image-paid", "category": "image", "output_modalities": ["image"], "pricing": {"request": 0}, "paid_only": True},
        "image",
    )
    assert generator._is_free_tier_model(
        {"name": "video-free", "category": "video", "output_modalities": ["video"], "pricing": {"second": 0}},
        "video",
    )
    assert generator._is_free_tier_model(
        {"name": "elevenmusic", "category": "audio", "output_modalities": ["audio"], "pricing": {"second": 0}},
        "music",
    )
    assert not generator._is_free_tier_model(
        {"name": "speech", "category": "audio", "output_modalities": ["audio"], "pricing": {"second": 0}},
        "music",
    )
    assert not generator._is_free_tier_model(
        {"name": "not-really-free", "category": "video", "output_modalities": ["video"], "pricing": {"second": 0.01}},
        "video",
    )


@pytest.mark.asyncio
async def test_generation_sends_key_only_in_header_and_saves_artifact(monkeypatch, tmp_path: Path):
    captured = {}

    class StreamResponse:
        status_code = 200
        headers = {"content-type": "image/png"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"png-content"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return StreamResponse()

    monkeypatch.setattr("deepdesk.media_generation.httpx.AsyncClient", lambda **_kwargs: FakeClient())
    generator = FreeMediaGenerator("private-key", tmp_path)

    result = await generator.generate("image", "a blue observatory")

    assert result["ok"] is True
    assert result["model"] == "legacy-flux"
    assert Path(result["path"]).read_bytes() == b"png-content"
    assert "private-key" not in captured["url"]
    assert captured["headers"] == {}
    assert captured["url"].startswith("https://image.pollinations.ai/prompt/")


@pytest.mark.asyncio
async def test_paid_model_is_refused_without_fallback(monkeypatch, tmp_path: Path):
    generator = FreeMediaGenerator("test-key", tmp_path)

    async def models(_kind, *, refresh=False):
        return [{"name": "free-one", "category": "image", "output_modalities": ["image"]}]

    monkeypatch.setattr(generator, "models", models)
    with pytest.raises(RuntimeError, match="paid fallback is disabled"):
        await generator.generate("image", "safe prompt", model="paid-one")


@pytest.mark.asyncio
async def test_official_free_video_and_music_models_do_not_require_pollinations_key(
    monkeypatch, tmp_path: Path
):
    generator = FreeMediaGenerator("", tmp_path)

    class FailingClient:
        async def __aenter__(self):
            raise OSError("optional Pollinations catalog is offline")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr("deepdesk.media_generation.httpx.AsyncClient", lambda **_kwargs: FailingClient())

    video = await generator.models("video", refresh=True)
    music = await generator.models("music", refresh=True)
    assert [item["name"] for item in video] == [
        "ltx-video-distilled-zerogpu",
        "ltx-video-2.3-zerogpu",
        "wan-2.1-public-demo",
        "easyanimate-video-modelscope",
    ]
    assert [item["name"] for item in music] == [
        "stable-audio-3-zerogpu",
        "ace-step-zerogpu",
        "musicgen-zerogpu",
        "stable-audio-open-zerogpu",
        "diffrhythm-zerogpu",
        "heartmula-zerogpu",
        "ezaudio-zerogpu",
        "inspiremusic-modelscope",
    ]
    assert all(item["pricing"]["request"] == 0 for item in [*video, *music])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "model_name", "extension", "content_type"),
    [
        ("video", "ltx-video-distilled-zerogpu", ".mp4", "video/mp4"),
        ("music", "stable-audio-3-zerogpu", ".wav", "audio/wav"),
    ],
)
async def test_zerogpu_generation_saves_cross_channel_artifact(
    monkeypatch,
    tmp_path: Path,
    kind: str,
    model_name: str,
    extension: str,
    content_type: str,
):
    generator = FreeMediaGenerator("", tmp_path / "outputs")
    downloaded = tmp_path / f"gradio-result{extension}"
    downloaded.write_bytes(b"generated-media")

    async def online(_space_id, *, refresh=False):
        return True, ""

    async def fake_to_thread(_callable, *_args):
        return ({"video": {"path": str(downloaded)}} if kind == "video" else (str(downloaded), {"seed": 1}))

    monkeypatch.setattr(generator, "_space_status", online)
    monkeypatch.setattr("deepdesk.media_generation.asyncio.to_thread", fake_to_thread)

    result = await generator.generate(kind, "a short clean test", model=model_name, duration=3)

    assert result["provider"] == "Hugging Face ZeroGPU"
    assert result["delivery_compatible"] == {"desktop": True, "feishu": True, "telegram": True}
    assert result["content_type"] == content_type
    assert Path(result["path"]).read_bytes() == b"generated-media"


@pytest.mark.asyncio
async def test_pollinations_style_secret_is_rejected_from_prompt(tmp_path: Path):
    generator = FreeMediaGenerator("", tmp_path)

    with pytest.raises(PermissionError, match="Credentials and secrets"):
        await generator.generate("image", "render sk_exampleSecretValue123456")


def test_huggingface_free_token_is_passed_only_to_gradio_client(monkeypatch, tmp_path: Path):
    captured = {}

    class Job:
        def result(self, timeout):
            captured["timeout"] = timeout
            return "generated.wav"

    class Client:
        def __init__(self, space_id, hf_token=None, verbose=True):
            captured["space_id"] = space_id
            captured["hf_token"] = hf_token
            captured["verbose"] = verbose

        def submit(self, *_args, **_kwargs):
            return Job()

    monkeypatch.setattr("gradio_client.Client", Client)
    generator = FreeMediaGenerator(
        "", tmp_path, huggingface_token="hf_private_test_token"
    )
    selected = next(
        item
        for item in generator._official_space_models("music")
        if item["name"] == "musicgen-zerogpu"
    )

    generator._run_space_generation(selected, "music", "rock", 512, 512, 8, 1)

    assert captured["space_id"] == "facebook/MusicGen"
    assert captured["hf_token"] == "hf_private_test_token"
    assert captured["verbose"] is False


def test_space_type_error_is_reported_as_local_adapter_failure():
    error = FreeMediaGenerator._space_error(
        TypeError("Client.__init__() got an unexpected keyword argument 'token'")
    )

    assert "local Gradio client" in str(error)
    assert "Hugging Face outage" not in str(error)


def test_huggingface_token_parameter_tracks_new_gradio_signature(monkeypatch, tmp_path: Path):
    captured = {}

    class Job:
        def result(self, timeout):
            return "generated.wav"

    class FutureClient:
        def __init__(self, _src, token=None, verbose=True):
            captured["token"] = token
            captured["verbose"] = verbose

        def submit(self, *_args, **_kwargs):
            return Job()

    monkeypatch.setattr("gradio_client.Client", FutureClient)
    generator = FreeMediaGenerator("", tmp_path, huggingface_token="hf_future_test")
    selected = next(
        item for item in generator._official_space_models("music")
        if item["name"] == "musicgen-zerogpu"
    )

    generator._run_space_generation(selected, "music", "rock", 512, 512, 8, 1)

    assert captured == {"token": "hf_future_test", "verbose": False}


@pytest.mark.parametrize(
    ("model_name", "expected_api"),
    [
        ("ltx-video-2.3-zerogpu", "/generate_video"),
        ("stable-audio-open-zerogpu", "/predict"),
        ("diffrhythm-zerogpu", "/infer_music"),
        ("heartmula-zerogpu", "/generate_music"),
        ("ezaudio-zerogpu", "/generate_audio"),
    ],
)
def test_new_free_space_adapters_use_their_published_api(
    monkeypatch, tmp_path: Path, model_name: str, expected_api: str
):
    captured = {}

    class Job:
        def result(self, timeout):
            captured["timeout"] = timeout
            return "generated-media.wav"

    class Client:
        def __init__(self, _space_id, **_kwargs):
            pass

        def submit(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return Job()

    monkeypatch.setattr("gradio_client.Client", Client)
    generator = FreeMediaGenerator("", tmp_path)
    kind = "video" if model_name.startswith("ltx-video") else "music"
    selected = next(
        item for item in generator._official_space_models(kind)
        if item["name"] == model_name
    )

    generator._run_space_generation(selected, kind, "energetic rock", 512, 512, 8, 7)

    assert captured["kwargs"]["api_name"] == expected_api
    assert captured["timeout"] >= 1200


def test_wan_public_demo_uses_independent_async_status_pool(monkeypatch, tmp_path: Path):
    generated = tmp_path / "wan-result.mp4"
    generated.write_bytes(b"video")
    calls = []

    class Client:
        def __init__(self, _space_id, **_kwargs):
            pass

        def predict(self, **kwargs):
            calls.append(kwargs)
            if kwargs["api_name"] == "/t2v_generation_async":
                return (1, 2)
            return {"video": {"path": str(generated)}}

    monkeypatch.setattr("gradio_client.Client", Client)
    generator = FreeMediaGenerator("", tmp_path)
    selected = next(
        item for item in generator._official_space_models("video")
        if item["name"] == "wan-2.1-public-demo"
    )

    result = generator._run_space_generation(
        selected, "video", "a person dancing", 1280, 720, 5, 11
    )

    assert result["video"]["path"] == str(generated)
    assert [item["api_name"] for item in calls] == [
        "/t2v_generation_async",
        "/status_refresh",
    ]
    assert generator._provider_pool(selected) == "wan-public-demo"


@pytest.mark.asyncio
async def test_media_tool_requires_exact_artifact_path_in_description(monkeypatch, tmp_path: Path):
    generator = FreeMediaGenerator("", tmp_path)

    async def no_models(_kind, *, refresh=False):
        return []

    monkeypatch.setattr(generator, "models", no_models)
    tool = MediaGenerationTool(generator)
    status = await tool.execute(
        {"action": "status"},
        ToolContext(task_id="task", workspace=str(tmp_path)),
    )
    assert status["free_only"] is True
    assert "exact returned path" in tool.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "extension", "content_type"),
    [("image", ".png", "image/png"), ("video", ".mp4", "video/mp4"), ("music", ".wav", "audio/wav")],
)
async def test_mainland_china_media_uses_modelscope_and_keeps_cross_channel_delivery(
    monkeypatch, tmp_path: Path, kind: str, extension: str, content_type: str
):
    generator = FreeMediaGenerator("", tmp_path / "outputs")
    generated = tmp_path / f"regional{extension}"
    generated.write_bytes(b"regional-media")

    async def online(_studio_id):
        return True, ""

    async def fake_to_thread(_callable, *_args):
        return {"path": str(generated)}

    monkeypatch.setattr(generator, "_modelscope_studio_status", online)
    monkeypatch.setattr("deepdesk.media_generation.asyncio.to_thread", fake_to_thread)

    result = await generator.generate(
        kind,
        "中国出口免费媒体测试",
        duration=3,
        egress_country="CN",
    )

    assert result["provider"] == "ModelScope shared Studio"
    assert result["egress_route"] == "mainland-china"
    assert result["content_type"] == content_type
    assert result["delivery_compatible"] == {"desktop": True, "feishu": True, "telegram": True}


@pytest.mark.asyncio
async def test_mainland_china_media_status_exposes_only_domestic_models(monkeypatch, tmp_path: Path):
    generator = FreeMediaGenerator("", tmp_path)

    async def online(_model, *, refresh=False):
        return True, ""

    monkeypatch.setattr(generator, "_modelscope_model_status", online)
    status = await generator.status(egress_country="CN")

    assert status["egress_route"] == "mainland-china"
    assert status["provider"] == "ModelScope mainland-China shared Studios"
    assert status["availability"]["image"]["models"] == ["easyanimate-image-modelscope"]
    assert status["availability"]["video"]["models"] == ["easyanimate-video-modelscope"]
    assert status["availability"]["music"]["models"] == ["inspiremusic-modelscope"]


@pytest.mark.asyncio
async def test_modelscope_status_requires_generation_endpoint_reachability(
    monkeypatch, tmp_path: Path
):
    generator = FreeMediaGenerator("", tmp_path)

    async def metadata_online(_studio_id):
        return True, ""

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            raise OSError("TLS route unavailable")

    monkeypatch.setattr(generator, "_modelscope_studio_status", metadata_online)
    monkeypatch.setattr(
        "deepdesk.media_generation.httpx.AsyncClient",
        lambda **_kwargs: FailingClient(),
    )

    available, reason = await generator._modelscope_model_status(
        generator._china_models("video")[0], refresh=True
    )

    assert available is False
    assert "reports Running" in reason
    assert "current network" in reason


@pytest.mark.asyncio
async def test_automatic_video_generation_falls_back_to_modelscope_after_zerogpu_quota(
    monkeypatch, tmp_path: Path
):
    generator = FreeMediaGenerator("", tmp_path)
    attempts = []

    async def failed_space(selected, *_args):
        attempts.append(selected["name"])
        raise RuntimeError("The shared free ZeroGPU quota is temporarily exhausted")

    async def successful_modelscope(selected, *_args):
        attempts.append(selected["name"])
        return {
            "ok": True,
            "provider": "ModelScope shared Studio",
            "model": selected["name"],
        }

    monkeypatch.setattr(generator, "_generate_with_space", failed_space)
    monkeypatch.setattr(generator, "_generate_with_modelscope", successful_modelscope)

    result = await generator.generate("video", "a dancer on a neon stage")

    assert attempts == [
        "ltx-video-distilled-zerogpu",
        "wan-2.1-public-demo",
        "easyanimate-video-modelscope",
    ]
    assert result["provider"] == "ModelScope shared Studio"
    assert result["automatic_free_fallback"] is True
    assert result["fallback_attempts"][0]["provider"] == "Hugging Face ZeroGPU"


@pytest.mark.asyncio
async def test_music_quota_skips_other_models_in_same_pool_then_uses_modelscope(
    monkeypatch, tmp_path: Path
):
    generator = FreeMediaGenerator("", tmp_path)
    attempts = []

    async def failed_space(selected, *_args):
        attempts.append(selected["name"])
        raise RuntimeError("The shared free ZeroGPU quota is temporarily exhausted")

    async def successful_modelscope(selected, *_args):
        attempts.append(selected["name"])
        return {
            "ok": True,
            "provider": "ModelScope shared Studio",
            "model": selected["name"],
        }

    monkeypatch.setattr(generator, "_generate_with_space", failed_space)
    monkeypatch.setattr(generator, "_generate_with_modelscope", successful_modelscope)

    result = await generator.generate("music", "energetic instrumental rock")

    assert attempts == ["stable-audio-3-zerogpu", "inspiremusic-modelscope"]
    assert result["model"] == "inspiremusic-modelscope"
    assert [item["model"] for item in result["fallback_attempts"]] == [
        "stable-audio-3-zerogpu",
        "ace-step-zerogpu",
        "musicgen-zerogpu",
        "stable-audio-open-zerogpu",
        "diffrhythm-zerogpu",
        "heartmula-zerogpu",
        "ezaudio-zerogpu",
    ]
