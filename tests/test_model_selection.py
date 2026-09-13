import pytest

from deepdesk.model_selection import ModelSelectionError, resolve_model_selection

OPTIONS = [
    {
        "selector": "aicodemirror-openai:gpt-5.6-sol",
        "provider": "aicodemirror-openai",
        "model": "gpt-5.6-sol",
    },
    {
        "selector": "aicodemirror-openai:gpt-5.6-terra",
        "provider": "aicodemirror-openai",
        "model": "gpt-5.6-terra",
    },
]


def test_spoken_model_name_with_spacing_resolves_exactly():
    assert resolve_model_selection("GPT 5.6 Sol", OPTIONS) == OPTIONS[0]["selector"]


def test_voice_near_miss_requires_confirmation_instead_of_auto():
    with pytest.raises(ModelSelectionError) as caught:
        resolve_model_selection("GPT5.6SOU", OPTIONS)

    payload = caught.value.payload()
    assert payload["code"] == "model_confirmation_required"
    assert payload["suggestions"][0]["selector"] == OPTIONS[0]["selector"]
    assert "No setting was changed" in payload["message"]


def test_unknown_model_never_falls_back_to_auto():
    with pytest.raises(ModelSelectionError) as caught:
        resolve_model_selection("totally unknown", OPTIONS)
    assert caught.value.payload()["code"] == "unsupported_model_selection"


def test_explicit_auto_remains_valid():
    assert resolve_model_selection("auto", OPTIONS) == "auto"
