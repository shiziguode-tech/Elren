"""Cold-page retries and history reads must not stall unrelated UI requests."""

from __future__ import annotations

import ast
import asyncio
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_frontend_high_priority import _function_source

ROOT = Path(__file__).resolve().parents[1]


def run_snapshot_script(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend snapshot regressions")
    source = (ROOT / "deepdesk/static/app.js").read_text("utf-8")
    function = _function_source(source, "requestSettingsSnapshot")
    program = (
        """
let settingsSnapshotPromise=null, settingsSnapshotStartedAt=0, now=0, calls=0;
const SETTINGS_SNAPSHOT_REUSE_MS=15000;
Date.now=()=>now;
"""
        + function
        + "\n"
        + body
    )
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return json.loads(result.stdout)


def test_failed_settings_prefetch_can_retry_immediately():
    result = run_snapshot_script("""
function api(){calls++; return calls===1 ? Promise.reject(new Error('offline')) : Promise.resolve({ok:true});}
(async()=>{
  let originalFailure='';
  try { await requestSettingsSnapshot(); } catch(error) {originalFailure=error.message;}
  const cleared=settingsSnapshotPromise===null && settingsSnapshotStartedAt===0;
  now=1000;
  const recovered=await requestSettingsSnapshot();
  process.stdout.write(JSON.stringify({originalFailure,cleared,recovered,calls}));
})();
""")
    assert result == {
        "originalFailure": "offline",
        "cleared": True,
        "recovered": {"ok": True},
        "calls": 2,
    }


def test_pending_and_successful_settings_snapshots_still_share_one_request():
    result = run_snapshot_script("""
let resolve;
function api(){calls++; return new Promise(done=>resolve=done);}
(async()=>{
  const first=requestSettingsSnapshot(); now=100;
  const second=requestSettingsSnapshot();
  resolve({model:'auto'}); await first;
  now=14000; const cached=requestSettingsSnapshot();
  process.stdout.write(JSON.stringify({same:first===second && first===cached,calls,value:await cached}));
})();
""")
    assert result == {"same": True, "calls": 1, "value": {"model": "auto"}}


def test_old_failed_snapshot_cannot_clear_newer_request():
    result = run_snapshot_script("""
const requests=[];
function api(){calls++; return new Promise((resolve,reject)=>requests.push({resolve,reject}));}
(async()=>{
  const old=requestSettingsSnapshot(); const observedOld=old.catch(()=>{});
  now=15001; const current=requestSettingsSnapshot();
  requests[0].reject(new Error('old failure')); await observedOld;
  const retained=settingsSnapshotPromise===current && settingsSnapshotStartedAt===15001;
  now=15002; const reused=requestSettingsSnapshot()===current;
  requests[1].resolve({ok:true}); await current;
  process.stdout.write(JSON.stringify({retained,reused,calls}));
})();
""")
    assert result == {"retained": True, "reused": True, "calls": 2}


def actual_list_endpoint(store):
    source = ast.parse((ROOT / "deepdesk/main.py").read_text("utf-8"))
    endpoint = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "list_tasks"
    )
    endpoint.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[endpoint], type_ignores=[]))
    scope = {"asyncio": asyncio, "task_store": store}
    # Execute only the repository-owned route function, never supplied content.
    exec(compile(module, "<actual-list-tasks-endpoint>", "exec"), scope)  # noqa: S102
    return scope["list_tasks"]


def test_history_read_runs_off_loop_and_keeps_response_semantics():
    event_loop_thread = threading.get_ident()
    calls = []
    released = threading.Event()
    item = SimpleNamespace(
        id="qa",
        title="Synthetic",
        pinned=False,
        prompt="Never send",
        status="completed",
        agent_profile="general",
        active_model="auto",
        source="web",
        created_at="created",
        updated_at="updated",
    )

    class Store:
        def list_page(self, **kwargs):
            assert threading.get_ident() != event_loop_thread
            calls.append(kwargs)
            # Only an event-loop callback releases the simulated slow read.
            # A direct synchronous call would prevent the callback from running.
            assert released.wait(2), "History read blocked the event loop"
            return [item], 40

    async def exercise():
        asyncio.get_running_loop().call_later(0.01, released.set)
        return await actual_list_endpoint(Store())(
            limit=500, offset=-2, query="QA", status="completed"
        )

    result = asyncio.run(exercise())
    assert calls == [{"limit": 500, "offset": -2, "query": "QA", "status": "completed"}]
    assert result == {
        "tasks": [vars(item)],
        "total": 40,
        "offset": 0,
        "limit": 100,
        "has_more": True,
    }


def test_history_worker_errors_still_propagate():
    class Store:
        def list_page(self, **_kwargs):
            raise ValueError("synthetic storage failure")

    with pytest.raises(ValueError, match="synthetic storage failure"):
        asyncio.run(actual_list_endpoint(Store())())


def test_frontend_uses_the_new_startup_cache_version():
    assert "/static/app.js?v=264" in (ROOT / "deepdesk/static/index.html").read_text("utf-8")
