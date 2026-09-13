from deepdesk.vision_runtime import AICODEMIRROR_VISION_MODEL, VisionRuntime


def test_default_vision_relay_uses_current_user_selected_flash(tmp_path):
    runtime = VisionRuntime("", "", "", False, "", 1, tmp_path, relay_api_key="fake")
    assert AICODEMIRROR_VISION_MODEL == "gemini-3.8-flash"
    assert runtime.relay_model == "gemini-3.8-flash"
    assert runtime.semantic_fallback_endpoints()[0].model == "gemini-3.8-flash"


def test_explicit_vision_relay_model_is_preserved(tmp_path):
    runtime = VisionRuntime(
        "", "", "", False, "", 1, tmp_path, relay_api_key="fake", relay_model="custom-vision",
    )
    assert runtime.semantic_fallback_endpoints()[0].model == "custom-vision"
