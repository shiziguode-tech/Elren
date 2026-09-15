from deepdesk.engine import RUNTIME_MOBILE_PROMPT, runtime_system_prompt
from deepdesk.models import AgentProfile


def test_mobile_policy_requires_result_verification_not_blind_retry():
    prompt = runtime_system_prompt("用手机微信给王剑发送你好", AgentProfile.GENERAL)
    assert "After sending, verify the device-side result" in prompt
    assert "continue proportionate read-only observation" in prompt
    assert "duplicate submission, NOT result verification" in prompt
    assert "Never resend as a test" in prompt
    assert "recipient read receipts" in prompt
    assert "make ONE targeted additional check" not in prompt


def test_policy_stops_unproductive_observation_without_false_success():
    assert "when further checks cannot resolve it" in RUNTIME_MOBILE_PROMPT
    assert "report remaining uncertainty" in RUNTIME_MOBILE_PROMPT
    assert "A tap being accepted does not establish sending" in RUNTIME_MOBILE_PROMPT
