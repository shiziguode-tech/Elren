"""Language reload owns a one-use, bounded transfer, not a general draft cache."""
import hashlib
import json
import os
import shutil
import subprocess

import pytest
from test_frontend_high_priority import APP_JS, _function_source


def run_js(body, *, timezone=None):
    source = APP_JS.read_text("utf-8")
    functions = "\n".join(_function_source(source, name) for name in (
        "saveLanguageComposerTransfer", "restoreLanguageComposerTransfer",
        "syncReasoningAvailability", "parseScheduleDateTime", "localizedConstraintMessage",
    ))
    script = """const fields = {
 '#prompt': {value:'CURRENT private draft',dataset:{}},
 '#modelPreference': {value:'deepseek:vision',dataset:{}},
 '#reasoningPreference': {value:'1',dataset:{},closest(){return {dataset:{}}}},
};
const $ = key => fields[key];
let taskId='A', continuationTaskId=null, currentTaskSnapshot=null;
let composerDraftScope='A', pendingAttachments=[{name:'谱.png',path:'screenshots/a.png',secret:'must not copy'}];
let composerDrafts=new Map([[null,{text:'new task draft',systemDraft:'',attachments:[]}],['B',{text:'other draft',systemDraft:'',attachments:[]}]]);
let newTaskModelPreference='other:model',newTaskReasoningPreference='medium',newTaskProjectPath='';
let pendingLanguageSwitchModel='',reasoningDefaultLoaded=false,uiLanguage='en';
let activeReasoningLevels=['auto','high','max'], selectedReasoning='max';
const reasoningPreferenceValue=()=>selectedReasoning;
const reasoningCapability=()=>({supported:true,levels:['high','max'],control:'real'});
const syncReasoningDots=()=>{},setReasoningPreference=value=>{selectedReasoning=value;};
const uiText=(zh,en)=>uiLanguage==='en'?en:zh;
let rendered=0;
const renderAttachments=()=>rendered++;
const storage=new Map();
const window={crypto:{randomUUID:()=> 'random-test-token'},
 sessionStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 location:{href:'http://127.0.0.1:8765/?lang=zh&task=A'},
 history:{replaceState:(_a,_b,url)=>{window.location.href=url;}}
};
function transfer() {
 const next=new URL('http://127.0.0.1:8765/?lang=en&task=A');
 saveLanguageComposerTransfer(next); window.location.href=next.toString();
 return next;
}
function freshPage() {
 composerDrafts=new Map();composerDraftScope=null;pendingAttachments=[];
 fields['#prompt'].value='';fields['#modelPreference'].value='auto';
 newTaskModelPreference='auto';newTaskReasoningPreference='auto';newTaskProjectPath='';
 selectedReasoning='high';
}
"""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    result = subprocess.run([node, "-e", script + functions + "\n" + body], capture_output=True,
                            text=True, encoding="utf-8", timeout=10, check=False,
                            env={**os.environ, **({"TZ": timezone} if timezone else {})})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_actual_transfer_restores_multiple_drafts_attachments_and_manual_preferences():
    result = run_js("""
const url=transfer(); const raw=[...storage.values()][0]; freshPage();
const restored=restoreLanguageComposerTransfer();
syncReasoningAvailability('old:model','high');
console.log(JSON.stringify({restored,scope:composerDraftScope,text:$('#prompt').value,
 attachments:pendingAttachments,drafts:[...composerDrafts.keys()],reasoning:selectedReasoning,
 newTaskModelPreference,newTaskReasoningPreference,model:pendingLanguageSwitchModel,
 storage:storage.size,url:window.location.href,rawContainsSecret:raw.includes('must not copy'),
 urlContainsDraft:url.toString().includes('private draft'),again:restoreLanguageComposerTransfer()}));
""")
    assert result == {"restored": True, "scope": "A", "text": "CURRENT private draft",
                         "attachments": [{"name": "谱.png", "path": "screenshots/a.png"}],
                         "drafts": [None, "B", "A"], "reasoning": "max", "newTaskModelPreference": "other:model",
                         "newTaskReasoningPreference": "medium", "model": "deepseek:vision", "storage": 0,
                         "url": "http://127.0.0.1:8765/?lang=en&task=A", "rawContainsSecret": False,
                         "urlContainsDraft": False, "again": False}


@pytest.mark.parametrize("mutation", [
    "s.createdAt-=300001", "s.createdAt+=100000", "s.token='wrong'", "s.version=2",
    "s.language='zh'", "s.task='B'", "s.scope='B'", "s.drafts.push(s.drafts[0])",
    "s.drafts[0][1].attachments=[{}]", "s.drafts[0][1].text={}",
    "s.model={}", "s.drafts=null",
])
def test_invalid_transfers_are_consumed_atomically_without_partial_restore(mutation):
    result = run_js("""
transfer(); const key=[...storage.keys()][0];const s=JSON.parse(storage.get(key));
""" + mutation + """;
storage.set(key,JSON.stringify(s));freshPage();
console.log(JSON.stringify({ok:restoreLanguageComposerTransfer(),count:composerDrafts.size,
 text:$('#prompt').value,storage:storage.size}));
""")
    assert result == {"ok": False, "count": 0, "text": "", "storage": 0}


def test_transfer_capacity_rejects_instead_of_truncating_content():
    result = run_js("""
$('#prompt').value='x'.repeat(2*1024*1024);
let threw=false;try{transfer()}catch{threw=true}
console.log(JSON.stringify({threw,storage:storage.size,length:$('#prompt').value.length}));
""")
    assert result == {"threw": True, "storage": 0, "length": 2097152}


def test_manual_choice_wins_over_restored_reasoning_after_user_changes_it():
    result = run_js("""
transfer();freshPage();restoreLanguageComposerTransfer();
syncReasoningAvailability('old','high');const restored=selectedReasoning;
delete $('#reasoningPreference').dataset.languageResumeReasoning;
selectedReasoning='high';syncReasoningAvailability();
console.log(JSON.stringify({restored,changed:selectedReasoning}));
""")
    assert result == {"restored": "max", "changed": "high"}


def test_handlers_reject_in_flight_work_and_storage_failure_before_discard_or_reload():
    source = APP_JS.read_text("utf-8")
    handler = source.split('$("#languageToggle").onclick = async () => {', 1)[1].split(
        'window.addEventListener("beforeunload"', 1)[0]
    assert "uploadRequestPending || startRequestPending" in handler
    assert handler.index("saveLanguageComposerTransfer(nextUrl)") < handler.index("discardSettingsChanges()")
    assert handler.index("saveLanguageComposerTransfer(nextUrl)") < handler.index("window.location.assign")
    assert "Language switch cancelled" in handler
    assert "languageSwitchPending = false" in handler


@pytest.mark.parametrize(("value", "valid"), [
    ("", False), ("2026", False), ("2026-09", False), ("2026-09-06 21:", False),
    ("2026-02-29 12:30", False), ("2026-02-31 12:30", False), ("2024-02-29 12:30", True),
    ("2026-13-01 12:30", False), ("2026-00-01 12:30", False), ("2026-01-00 12:30", False),
    ("2026-09-06 24:00", False), ("2026-09-06 12:60", False), ("0000-01-01 12:30", False),
    ("2026-09-06T21:30", True), ("2026-09-06 21:30", True), ("0099-01-01 12:30", True),
])
def test_editable_date_parser_rejects_partial_and_rollover_dates(value, valid):
    assert run_js(f"console.log(JSON.stringify(Boolean(parseScheduleDateTime({json.dumps(value)}))));") is valid


def test_date_validation_owns_partial_value_message_in_both_languages():
    result = run_js("""
const field={value:'2026-',hasAttribute:()=>true,validity:{}};
const en=localizedConstraintMessage(field);uiLanguage='zh';
console.log(JSON.stringify({en,zh:localizedConstraintMessage(field),text:field.value}));
""")
    assert result == {"en": "Enter a valid date and time: YYYY-MM-DD HH:mm.",
                         "zh": "请输入有效日期与时间，格式：YYYY-MM-DD HH:mm。", "text": "2026-"}


def test_nonexistent_dst_local_time_is_rejected_not_silently_shifted():
    result = run_js("""
console.log(JSON.stringify(['2026-03-08 01:30','2026-03-08 02:30','2026-03-08 03:30']
 .map(value=>Boolean(parseScheduleDateTime(value)))));
""", timezone="America/New_York")
    assert result == [True, False, True]


@pytest.mark.parametrize("active", [True, False])
def test_transfer_across_separate_javascript_processes(active):
    setup = "" if active else """
composerDraftScope=null;window.location.href='http://127.0.0.1:8765/?lang=zh';
transfer=()=>{const next=new URL('http://127.0.0.1:8765/?lang=en');
saveLanguageComposerTransfer(next);window.location.href=next.toString();};
"""
    saved = run_js(setup + """
transfer();console.log(JSON.stringify({url:window.location.href,raw:[...storage.values()][0]}));
""")
    result = run_js(f"const transferred={json.dumps(saved)};" + """
freshPage();window.location.href=transferred.url;
storage.set('elren.language-composer-transfer.v1',transferred.raw);
const ok=restoreLanguageComposerTransfer();
console.log(JSON.stringify({ok,text:$('#prompt').value,scope:composerDraftScope,
reasoning:$('#reasoningPreference').dataset.languageResumeReasoning,storage:storage.size}));
""")
    assert result == {"ok": True, "text": "CURRENT private draft", "scope": "A" if active else None,
                         "reasoning": "max", "storage": 0}


def test_calendar_vendor_is_pinned_licensed_and_loaded_from_local_files():
    static = APP_JS.parent
    vendor = static / "vendor/air-datepicker"
    manifest = json.loads((vendor / "UPSTREAM.json").read_text("utf-8"))
    assert manifest["version"] == "3.6.0"
    assert "MIT License" in (vendor / "LICENSE.md").read_text("utf-8")
    for relative, expected in manifest["files"].items():
        assert hashlib.sha256((vendor / relative).read_bytes()).hexdigest() == expected
        assert (vendor / relative).read_bytes().endswith(b"\n")
        assert hashlib.sha256((vendor / relative).read_bytes()[:-1]).hexdigest() == manifest["upstream_files"][relative]
    html = (static / "index.html").read_text("utf-8")
    assert html.index('air-datepicker.js?v=3.6.0') < html.index('/static/app.js?')
    assert html.index('locales-browser.js?v=3.6.0') < html.index('/static/app.js?')
    assert 'type="datetime-local"' not in html


@pytest.mark.parametrize("condition", ["upload", "send", "storage", "upload-during-confirm", "too-large"])
def test_actual_language_handler_never_reloads_or_discards_on_failed_transfer(condition):
    source = APP_JS.read_text("utf-8")
    handler = source.split('$("#languageToggle").onclick = ', 1)[1].split(
        'window.addEventListener("beforeunload"', 1)[0].strip()
    setup = {
        "upload": "uploadRequestPending=true;",
        "send": "startRequestPending=true;",
        "storage": "window.sessionStorage.setItem=()=>{throw Error('disabled')};",
        "upload-during-confirm": "askForConfirmation=async()=>{uploadRequestPending=true;return true;};",
        "too-large": "$('#prompt').value='x'.repeat(2*1024*1024);",
    }[condition]
    result = run_js("""
let languageSwitchPending=false,uploadRequestPending=false,startRequestPending=false,settingsFormDirty=true;
let discarded=0,reloaded=0,toasts=0;
let askForConfirmation=async()=>true;
const isEnglish=()=>true,discardSettingsChanges=()=>discarded++;
const latestStatus={},cacheRuntimeStatus=()=>{},syncDesktopLanguage=()=>{},showWorkspaceToast=()=>toasts++;
const LANGUAGE_STORAGE_KEY='lang';window.location.assign=()=>reloaded++;
""" + setup + "const handler=" + handler + """
handler().then(()=>console.log(JSON.stringify({discarded,reloaded,toasts,pending:languageSwitchPending,
 kept:Boolean($('#prompt').value),storage:storage.size})));
""")
    assert result == {"discarded": 0, "reloaded": 0, "toasts": 1, "pending": False, "kept": True, "storage": 0}
