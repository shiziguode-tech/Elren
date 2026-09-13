"""Compatible routes use synthetic keys and MockTransport only; no network."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from test_settings_atomicity import disk_snapshot
from test_settings_atomicity import isolated as isolated  # noqa: PLC0414 -- register pytest fixture

from deepdesk.custom_providers import normalize_custom_provider
from deepdesk.deepseek import DeepSeekClient, DeepSeekError
from deepdesk.provider_secrets import ProviderSecrets, ProviderSecretsStore
from deepdesk.runtime_settings import RuntimeSettingsPatch


def row(**changes):
    return {"id": "relay-one", "source": "custom", "provider": "openai",
            "model": "arbitrary-alias", "base_url": "https://relay.example/api/v1",
            "api_key": "synthetic-only", "reasoning_levels": ["low", "high"]} | changes


@pytest.mark.parametrize("url,expected", [
    ("https://relay.example", "https://relay.example/v1"),
    ("https://relay.example/api/v1/chat/completions", "https://relay.example/api/v1"),
    ("https://relay.example/custom/", "https://relay.example/custom"),
    ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1"),
])
def test_base_urls(url, expected):
    assert normalize_custom_provider(row(base_url=url))["base_url"] == expected


@pytest.mark.parametrize("changes", [
    {"base_url": ""}, {"base_url": "http://remote.example/v1"},
    {"base_url": "https://user:secret@relay.example/v1"},
    {"base_url": "https://relay.example/v1?key=hidden"},
    {"base_url": "https://relay.example/v1#fragment"},
    {"base_url": "https://relay.example/\nmalformed"},
    {"base_url": "https://relay.example/v1/messages"},
    {"base_url": "https://relay.example/v1/responses"},
    {"base_url": "file:///private"}, {"base_url": "https://relay.example:99999"},
    {"reasoning_levels": "high"}, {"reasoning_levels": ["ultra"]},
    {"provider": "anthropic", "reasoning_levels": ["minimal"]},
    {"model": ""}, {"api_key": "secret\r\nheader"},
])
def test_invalid_settings_rejected_before_commit(changes):
    with pytest.raises(ValidationError):
        RuntimeSettingsPatch(model_providers=[row(**changes)])


def test_duplicate_ids_rejected():
    with pytest.raises(ValidationError):
        RuntimeSettingsPatch(model_providers=[row(), row(base_url="https://other.example")])


def test_saved_keys_remain_endpoint_scoped(tmp_path):
    store = ProviderSecretsStore(tmp_path / "provider-secrets.vault", ProviderSecrets())
    store.update(model_providers=[row(), row(id="relay-two", base_url="https://other.example")])
    status = store.model_provider_status()
    assert {item["selector"] for item in status} == {"custom:relay-one", "custom:relay-two"}
    assert "synthetic-only" not in str(status)
    assert status[0]["reasoning_levels"] == ["low", "high"]
    store.update(model_providers=[row(api_key="", reasoning_levels=["medium"])])
    assert store.value.model_providers[0]["api_key"] == "synthetic-only"
    reloaded = ProviderSecretsStore(store.path, ProviderSecrets())
    assert reloaded.value.model_providers[0]["reasoning_levels"] == ["medium"]
    store.update(model_providers=[row(api_key="", base_url="https://different.example/v1")])
    assert store.value.model_providers[0]["api_key"] == ""
    assert not store.model_provider_status()[0]["configured"]


@pytest.mark.parametrize("change", [{"provider": "anthropic"}, {"model": "other"}])
def test_hidden_key_not_reused_after_target_change(change):
    normal = ProviderSecretsStore._normalize_model_providers
    assert normal([row(api_key="", **change)], [row()])[0]["api_key"] == ""


@pytest.mark.parametrize("protocol,model", [("openai", "gpt-6-astra"), ("openai", "any-alias"), ("anthropic", "any-alias")])
def test_actual_adapters_use_custom_url_headers_and_declared_effort(monkeypatch, protocol, model):
    client = DeepSeekClient("https://never-contact.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([row(provider=protocol, model=model)])
    endpoint = client._endpoint_for("custom:relay-one")
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        if protocol == "anthropic":
            return httpx.Response(200, json={"id": "mock", "model": model, "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    for effort in ("high", "auto", "medium"):
        token = client.bind_task_reasoning_effort(effort)
        try:
            result = asyncio.run(client._request(endpoint, "synthetic-only", [{"role": "user", "content": "test"}], []))
        finally:
            client.reset_task_reasoning_effort(token)
        assert result["choices"][0]["message"]["content"] == "ok"
        request = requests[-1]
        suffix = "/messages" if protocol == "anthropic" else "/chat/completions"
        assert str(request.url) == "https://relay.example/api/v1" + suffix
        payload = json.loads(request.content)
        assert payload["model"] == model
        if protocol == "anthropic":
            assert request.headers["x-api-key"] == "synthetic-only"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert payload.get("output_config") == ({"effort": "high"} if effort == "high" else None)
            assert "thinking" not in payload
            assert "reasoning_effort" not in payload
        else:
            assert request.headers["authorization"] == "Bearer synthetic-only"
            assert payload.get("reasoning_effort") == ("high" if effort == "high" else None)
    assert client.reasoning_capability(endpoint.selector).verification == "user_declared"
    assert client.model_options()[0]["context_window_source"] != "official_model_spec"
    client.set_provider_models([])
    with pytest.raises(DeepSeekError, match="no longer configured"):
        client._endpoint_for("custom:relay-one")


def test_unchecked_known_model_has_no_inferred_effort():
    client = DeepSeekClient("https://never-contact.example", "deepseek-v4-flash", "", "")
    client.set_provider_models([row(model="gpt-6-astra", reasoning_levels=[])])
    assert not client.reasoning_capability("custom:relay-one").public()["supported"]


def test_custom_keys_never_exported_as_official_tool_credentials():
    source = (Path(__file__).parents[1] / "deepdesk/main.py").read_text(encoding="utf-8")
    section = source.split("    def configured_model_key(provider: str) -> str:", 1)[1].split("    def configured_secret_values", 1)[0]
    assert 'if entry.get("source") == "custom" or "base_url" in entry:' in section
    assert section.index("continue") < section.index('key = str(entry.get("api_key")')


@pytest.mark.asyncio
async def test_settings_api_save_read_and_invalid_patch_are_atomic(isolated):
    app, http = isolated
    response = await http.patch("/api/settings", json={"model_providers": [row()], "model": "custom:relay-one", "reasoning_effort": "high"})
    assert response.status_code == 200, response.text
    saved = (await http.get("/api/settings")).json()
    assert saved["model"] == "custom:relay-one"
    custom = next(option for option in saved["available_models"] if option["selector"] == "custom:relay-one")
    assert custom["reasoning"]["levels"] == ["low", "high"]
    assert custom["reasoning"]["verification"] == "user_declared"
    assert "synthetic-only" not in str(saved)
    before = disk_snapshot(app)
    invalid = await http.patch("/api/settings", json={"model_providers": [row(base_url="https://bad.example?key=do-not-echo")], "theme_mode": "dark"})
    assert invalid.status_code == 422
    assert "synthetic-only" not in invalid.text
    assert "do-not-echo" not in invalid.text
    assert before == disk_snapshot(app)
