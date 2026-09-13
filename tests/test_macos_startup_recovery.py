from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_live_backend_after_startup_delay_keeps_recovery_polling():
    source = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    health = source.split("private func beginHealthCheck", 1)[1].split("private func showFailure", 1)[0]
    assert "StartupProbeState" in source
    assert "startupProbe.next" in health
    assert "case .showWaitingAndProbe:" in health
    assert "showStartupDelay()" in health
    assert "healthDeadline" not in health
    assert "backend?.isRunning == true" in health
    assert 'if ready {' in health
    assert 'self.webView.load(URLRequest' in health
    assert "generation == self.healthGeneration" in health
    assert "!self.isTerminating" in health


def test_startup_delay_message_is_localized_and_does_not_claim_permission_denial():
    source = (ROOT / "macos/ElrenMac.swift").read_text("utf-8")
    delay = source.split("private func showStartupDelay", 1)[1].split("private func showFailure", 1)[0]
    assert 'desktopLanguage == "zh"' in delay
    assert "Still waiting for the local service" in delay
    assert "服务就绪后会自动进入" in delay
    assert "healthTimer?.invalidate" not in delay
    assert "could not start" not in delay
