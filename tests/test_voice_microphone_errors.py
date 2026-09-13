import json
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def run_voice(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    source = (ROOT / "deepdesk/static/voice.js").read_text("utf-8")
    source = source.replace("  translateVoiceUi();", "  globalThis.qaVoice = { resumeRecognition, stopRecognition, startMeter, microphoneError, recognitionError };")
    setup = '''
const assert = require('node:assert/strict');
const nodes = new Map();
global.document = {querySelector(selector) {
 if (!nodes.has(selector)) nodes.set(selector, {open:true,dataset:{},textContent:'',
   setAttribute(){},addEventListener(){},style:{setProperty(){}}});
 return nodes.get(selector);
}};
global.isEnglish=()=>true;
let created=0, stopped=0, instances=[];
global.window={addEventListener(){},SpeechRecognition:class {
 constructor(){created++;instances.push(this);} start(){} stop(){this.onend?.();}
}};
let capture = async()=>({getTracks:()=>[{stop(){stopped++;}}]});
Object.defineProperty(global,'navigator',{value:{mediaDevices:{getUserMedia:(x)=>capture(x)}}});
const $=s=>document.querySelector(s);
'''
    script = setup + source + "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
    result = subprocess.run([node, "-e", script], check=False, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name, message", [
    ("NotFoundError", "No microphone detected"),
    ("DevicesNotFoundError", "No microphone detected"),
    ("NotAllowedError", "Microphone access is blocked"),
    ("NotReadableError", "microphone could not be read"),
    ("AbortError", "microphone could not be read"),
])
def test_capture_failures_are_distinct_and_do_not_start_dictation(name, message):
    run_voice(f'''
capture=async()=>{{throw {{name:{json.dumps(name)}}};}};
await qaVoice.resumeRecognition();
assert.equal(created,0);
assert.ok($('#voiceStatus').textContent.includes({json.dumps(message)}));
assert.equal($('#voiceOrb').dataset.state,'error');
capture=async()=>({{getTracks:()=>[{{stop(){{stopped++;}}}}]}});
await qaVoice.resumeRecognition();
assert.equal(created,1); assert.equal(stopped,1);
qaVoice.stopRecognition();
''')


def test_cancelled_capture_releases_tracks_and_cannot_restart_closed_dialog():
    run_voice('''
let resolveCapture;
capture=()=>new Promise(resolve=>{resolveCapture=resolve;});
const pending=qaVoice.resumeRecognition();
await qaVoice.resumeRecognition(); // no duplicate capture while waiting
qaVoice.stopRecognition(); $('#voiceDialog').open=false;
resolveCapture({getTracks:()=>[{stop(){stopped++;}}]});
await pending; assert.equal(created,0); assert.equal(stopped,1);
''')


def test_dictation_denial_is_not_falsely_labelled_as_microphone_denial():
    run_voice('''
await qaVoice.resumeRecognition();
instances[0].onerror({error:'not-allowed'});
assert.ok($('#voiceStatus').textContent.startsWith('Dictation is not authorized'));
assert.ok(!qaVoice.recognitionError('service-not-allowed').includes('Windows'));
await qaVoice.resumeRecognition();
const saved=$('#voiceStatus').textContent;
instances[0].onerror({error:'network'}); // stale recognizer cannot overwrite current UI
assert.equal($('#voiceStatus').textContent,saved);
qaVoice.stopRecognition();
''')


def test_late_meter_capture_is_stopped_after_pause():
    run_voice('''
await qaVoice.resumeRecognition();
let resolveCapture;
capture=()=>new Promise(resolve=>{resolveCapture=resolve;});
const pending=qaVoice.startMeter();
qaVoice.stopRecognition();
resolveCapture({getTracks:()=>[{stop(){stopped++;}}]});
await pending; assert.equal(stopped,2);
''')


def test_audio_entitlement_only_added_to_desktop_not_general_cli_tools():
    desktop = plistlib.loads((ROOT / "macos/Desktop.entitlements").read_bytes())
    general = plistlib.loads((ROOT / "macos/Elren.entitlements").read_bytes())
    assert desktop["com.apple.security.device.audio-input"] is True
    assert "com.apple.security.device.audio-input" not in general
    builder = (ROOT / "macos/build-macos-app.sh").read_text("utf-8")
    assert builder.count('--entitlements "$ROOT/macos/Desktop.entitlements"') == 2


def test_meter_setup_failure_releases_acquired_input():
    run_voice('''
await qaVoice.resumeRecognition();
window.AudioContext=class {constructor(){throw Error('audio context unavailable');}};
await qaVoice.startMeter();
assert.equal(stopped,2); // probe plus failed visual meter
qaVoice.stopRecognition();
''')


@pytest.mark.parametrize("stage", ["constructor", "start"])
def test_synchronous_dictation_failure_does_not_retry_forever(stage):
    run_voice('''
let scheduled=0;
global.setTimeout=()=>{scheduled++;return 1;};
window.SpeechRecognition=class {
 constructor(){if(STAGE==='constructor') throw {name:'NotAllowedError'};}
 start(){throw {name:'NotAllowedError'};} stop(){}
};
await qaVoice.resumeRecognition();
assert.equal(scheduled,0);
assert.equal($('#voiceOrb').dataset.state,'error');
assert.ok($('#voiceStatus').textContent.startsWith('Dictation is not authorized'));
qaVoice.stopRecognition();
'''.replace("STAGE", json.dumps(stage)))


def test_natural_end_invalidates_late_meter_before_automatic_restart():
    run_voice('''
global.setTimeout=()=>1;
await qaVoice.resumeRecognition();
let resolveCapture;
capture=()=>new Promise(resolve=>{resolveCapture=resolve;});
const pending=qaVoice.startMeter();
instances[0].onend();
resolveCapture({getTracks:()=>[{stop(){stopped++;}}]});
await pending; assert.equal(stopped,2);
qaVoice.stopRecognition();
''')
