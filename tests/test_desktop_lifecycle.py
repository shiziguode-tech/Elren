import asyncio
from pathlib import Path

import pytest

from deepdesk.desktop_lifecycle import serve_desktop, service_instance


@pytest.mark.parametrize("args,expected", [
    (["--elren-service-id", "a" * 32], "a" * 32),
    (["--elren-service-id=" + "b" * 32], "b" * 32),
    ([], None), (["--elren-service-id"], None),
    (["--elren-service-id", "../../other"], None),
    (["--elren-service-id", "A" * 32], None),
])
def test_instance_validation(args, expected):
    assert service_instance(args) == expected


@pytest.mark.asyncio
async def test_exit_waits_for_lifespan_and_ignores_other_instances(tmp_path: Path):
    instance = "a" * 32
    complete = tmp_path / f"service-exit-complete-{instance}"

    class Server:
        should_exit = False
        cleaned_up = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)
            assert not complete.exists()
            await asyncio.sleep(0.05)
            self.cleaned_up = True

    server = Server()
    task = asyncio.create_task(serve_desktop(server, tmp_path, instance))
    (tmp_path / ("service-exit-request-" + "b" * 32)).touch()
    (tmp_path / f"service-stop-intent-{instance}").touch()
    await asyncio.sleep(0.25)
    assert not server.should_exit
    (tmp_path / f"service-exit-request-{instance}").touch()
    await asyncio.wait_for(task, timeout=2)
    assert server.cleaned_up
    assert complete.read_text() == "stopped"


@pytest.mark.asyncio
async def test_failed_shutdown_is_not_acknowledged(tmp_path):
    instance = "a" * 32
    (tmp_path / f"service-exit-request-{instance}").touch()

    class Server:
        should_exit = False

        async def serve(self):
            await asyncio.sleep(0.05)
            raise RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await serve_desktop(Server(), tmp_path, instance)
    assert not (tmp_path / f"service-exit-complete-{instance}").exists()
    assert (tmp_path / f"service-exit-failed-{instance}").read_text() == "shutdown_failed"


@pytest.mark.asyncio
async def test_non_desktop_start_remains_supported(tmp_path):
    class Server:
        async def serve(self):
            self.ran = True

    server = Server()
    await serve_desktop(server, tmp_path, None)
    assert server.ran
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_real_uvicorn_closes_listener_and_finishes_cleanup(tmp_path):
    from contextlib import asynccontextmanager

    import uvicorn
    from fastapi import FastAPI

    cleaned = []

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.sleep(0.05)
        cleaned.append(True)

    server = uvicorn.Server(uvicorn.Config(
        FastAPI(lifespan=lifespan), host="127.0.0.1", port=0, log_level="error",
    ))
    instance = "c" * 32
    task = asyncio.create_task(serve_desktop(server, tmp_path, instance))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        (tmp_path / f"service-exit-request-{instance}").touch()
        await asyncio.wait_for(task, timeout=5)
        assert cleaned == [True]
        assert (tmp_path / f"service-exit-complete-{instance}").is_file()
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    finally:
        server.should_exit = True
        if not task.done():
            await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_real_uvicorn_consumed_lifespan_failure_is_not_success(tmp_path):
    from contextlib import asynccontextmanager

    import uvicorn
    from fastapi import FastAPI

    @asynccontextmanager
    async def lifespan(app):
        yield
        raise RuntimeError("deliberate isolated cleanup failure")

    server = uvicorn.Server(uvicorn.Config(
        FastAPI(lifespan=lifespan), host="127.0.0.1", port=0, log_level="critical",
    ))
    instance = "d" * 32
    complete = tmp_path / f"service-exit-complete-{instance}"
    task = asyncio.create_task(serve_desktop(server, tmp_path, instance))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        # Even a stale success result must not override an actual failure.
        complete.write_text("stale")
        (tmp_path / f"service-exit-request-{instance}").touch()
        with pytest.raises(RuntimeError, match="lifecycle cleanup failed"):
            await asyncio.wait_for(task, timeout=5)
        assert server.lifespan.shutdown_failed
        assert not complete.exists()
        assert (tmp_path / f"service-exit-failed-{instance}").read_text() == "shutdown_failed"
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    finally:
        server.should_exit = True
        if not task.done():
            await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_real_uvicorn_drains_live_stream_with_a_finite_deadline(tmp_path):
    from contextlib import asynccontextmanager

    import httpx
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    cleaned = asyncio.Event()
    stream_finished = asyncio.Event()
    release_stream = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app):
        yield
        cleaned.set()

    app = FastAPI(lifespan=lifespan)

    @app.get("/isolated-stream")
    async def stream_response():
        async def chunks():
            try:
                while not release_stream.is_set():
                    yield b"isolated stream\n"
                    await asyncio.sleep(0.05)
            finally:
                stream_finished.set()
        return StreamingResponse(chunks())

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="critical",
        timeout_graceful_shutdown=0.15,
    ))
    instance = "e" * 32
    task = asyncio.create_task(serve_desktop(server, tmp_path, instance))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(trust_env=False) as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/isolated-stream") as response:
                # Keep the iterator alive: otherwise its finalizer closes the
                # connection and this stops testing the live-response case.
                iterator = response.aiter_bytes()
                assert await anext(iterator)
                assert not stream_finished.is_set()
                (tmp_path / f"service-exit-request-{instance}").touch()
                await asyncio.wait_for(task, timeout=3)
                assert cleaned.is_set()
                await asyncio.wait_for(stream_finished.wait(), timeout=1)
                assert not release_stream.is_set()
                assert server.config.timeout_graceful_shutdown == 0.15
                assert (tmp_path / f"service-exit-complete-{instance}").read_text() == "stopped"
    finally:
        release_stream.set()
        server.should_exit = True
        if not task.done():
            await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_desktop_default_drain_timeout_and_invalid_id(tmp_path):
    from types import SimpleNamespace

    class Server:
        config = SimpleNamespace(timeout_graceful_shutdown=None)

        async def serve(self):
            self.ran = True

    server = Server()
    with pytest.raises(ValueError, match="Invalid desktop service instance"):
        await serve_desktop(server, tmp_path, "../not-an-instance")
    assert not getattr(server, "ran", False)
    assert not list(tmp_path.iterdir())
    await serve_desktop(server, tmp_path, "f" * 32)
    assert server.config.timeout_graceful_shutdown == 5.0


@pytest.mark.asyncio
async def test_success_marker_io_failure_is_recorded_as_failure(tmp_path, monkeypatch):
    instance = "a" * 32
    complete = tmp_path / f"service-exit-complete-{instance}"
    (tmp_path / f"service-exit-request-{instance}").touch()
    original_write = Path.write_text

    def fail_success_write(path, *args, **kwargs):
        if path == complete:
            raise OSError("isolated write failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_success_write)

    class Server:
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)

    with pytest.raises(OSError, match="isolated write failure"):
        await serve_desktop(Server(), tmp_path, instance)
    assert not complete.exists()
    assert (tmp_path / f"service-exit-failed-{instance}").read_text() == "shutdown_failed"
