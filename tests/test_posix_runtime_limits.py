import asyncio
from types import SimpleNamespace

import pytest

from deepdesk import posix_sandbox as module


@pytest.mark.asyncio
@pytest.mark.parametrize("usage,reason", [((20, 1), "process_count"), ((1, 300 * 1024 * 1024), "memory_rss")])
async def test_darwin_uses_owned_group_limits_not_unsupported_rlimits(tmp_path, monkeypatch, usage, reason):
    limit_calls, kills = [], []
    done = asyncio.Event()
    process = SimpleNamespace(pid=43210, returncode=None)
    async def communicate():
        await done.wait()
        return b"", b""
    process.communicate = communicate
    def killpg(pid, signal):
        kills.append(pid)
        process.returncode = -9
        done.set()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.os, "setsid", lambda: None, raising=False)
    monkeypatch.setattr(module.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(module, "resource", SimpleNamespace(
        RLIMIT_CPU=1, RLIMIT_AS=2, RLIMIT_NPROC=3,
        setrlimit=lambda kind, limits: limit_calls.append(kind)))
    monkeypatch.setattr(module.PosixResourceSandbox, "_group_usage", staticmethod(lambda pid: usage))
    async def create(*args, **kwargs):
        kwargs["preexec_fn"]()
        return process
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    result = await module.PosixResourceSandbox(tmp_path).run("synthetic", timeout_seconds=2)
    assert result["limit_exceeded"] == reason
    assert result["timed_out"] is False
    assert limit_calls == [1]
    assert kills == [43210]
    status = module.PosixResourceSandbox(tmp_path).status()
    assert status["enforced"]["address_space_limit"] is False
    assert status["enforced"]["process_limit"] is False
