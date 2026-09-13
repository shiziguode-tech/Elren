"""Execute production diagnostics handlers with deferred synthetic requests."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "deepdesk/static/app.js").read_text("utf-8")
HANDLERS = SOURCE[SOURCE.index("let capabilityRequestGeneration = 0;"):SOURCE.index("function formatBytes(")]
PRELUDE = r"""
const assert = require('node:assert/strict');
const elements=new Map(), requests=[];
const $=id=>{if(!elements.has(id))elements.set(id, {open:false,innerHTML:'',onclick:null,
  showModal(){this.open=true},close(){this.open=false},addEventListener(){}});return elements.get(id)};
const api=url=>new Promise((resolve,reject)=>requests.push({url,resolve,reject}));
const uiText=(zh,en)=>en,isEnglish=()=>true,escapeHtml=s=>String(s),localizeKnownSystemMessage=s=>s;
const setPrimaryNavigation=()=>{},focusMainContentTarget=()=>{},visionCapabilityMarkup=()=>'';
const groupLocalCapabilities=tools=>[{id:'deepdesk',tools}],localizedToolName=s=>s,capabilityGroupTitle=()=>'';
let latestStatus={}; const loadStatus=async()=>{};
const tick=async()=>{for(let i=0;i<10;i++)await Promise.resolve()};
"""


def run_js(body):
    result = subprocess.run([str(ROOT / "work/node-runtime/node.exe"), "-e",
                             PRELUDE + HANDLERS + "\n(async()=>{" + body + "})().catch(e=>{console.error(e);process.exitCode=1})"],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("catalog,plugins", [({}, {}), ({"groups": []}, {}),
    ({"groups": {}}, {"loaded": 0, "total": 0}),
    ({"groups": []}, {"loaded": -1, "total": 0}),
    ({"groups": []}, {"loaded": 5, "total": 2}),
    ({"groups": []}, {"loaded": "1", "total": 2})])
def test_partial_catalog_preserves_local_tools_without_undefined(catalog, plugins):
    run_js("""
const pending=$('#openCapabilities').onclick();
requests[0].resolve({tools:[{name:'LOCAL_TOOL'}]}); await tick();
requests[1].resolve(CATALOG); requests[2].resolve(PLUGINS); await pending;
const html=$('#capabilityContent').innerHTML;
assert.ok(html.includes('LOCAL_TOOL'));
assert.ok(html.includes('OpenClaw is offline'));
assert.ok(!html.includes('undefined'));
""".replace("CATALOG", json.dumps(catalog)).replace("PLUGINS", json.dumps(plugins)))


@pytest.mark.parametrize("late_error", [False, True])
def test_close_reopen_ignores_late_status_success_and_error(late_error):
    run_js("""
const old=$('#openCapabilities').onclick(); closeCapabilityDialog();
const current=$('#openCapabilities').onclick();
requests[1].resolve({tools:[{name:'NEW_TOOL'}]}); await tick();
requests[2].resolve({groups:[]}); requests[3].resolve({loaded:0,total:0}); await current;
const expected=$('#capabilityContent').innerHTML;
LATE_RESPONSE;
await old;
assert.equal($('#capabilityContent').innerHTML, expected);
assert.equal(latestStatus.tools[0].name,'NEW_TOOL');
""".replace("LATE_RESPONSE", "requests[0].reject(new Error('old failure'))" if late_error else
            "requests[0].resolve({tools:[{name:'OLD_TOOL'}]})"))


def test_close_reopen_ignores_old_extension_catalog():
    run_js("""
const old=$('#openCapabilities').onclick();
requests[0].resolve({tools:[{name:'OLD_LOCAL'}]}); await tick();
closeCapabilityDialog(); const current=$('#openCapabilities').onclick();
requests[3].resolve({tools:[{name:'NEW_LOCAL'}]}); await tick();
requests[4].resolve({groups:[]}); requests[5].resolve({loaded:0,total:0}); await current;
const expected=$('#capabilityContent').innerHTML;
requests[1].resolve({groups:[{id:'old',tools:[{name:'OLD_EXTENSION'}]}]});
requests[2].resolve({loaded:1,total:1}); await old;
assert.equal($('#capabilityContent').innerHTML,expected);
""")
