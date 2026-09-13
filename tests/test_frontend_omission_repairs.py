"""Real frontend handlers: origin ownership, discard, and pagination failures.

No service, credentials, network, or real uploads are used. Deferred API calls
and DOM-only fixtures exercise production function bodies, not copied reducers.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "deepdesk/static/app.js").read_text("utf-8")
INDEX = (ROOT / "deepdesk/static/index.html").read_text("utf-8")


def function(name):
    match = re.search(rf"^(?:async )?function {name}\([\s\S]*?^}}", SOURCE, re.MULTILINE)
    assert match, name
    return match.group()


SETTINGS_REPAIR_HELPERS = (
    "settingsPreferenceFields", "settingsPreferenceValues", "changedSettingsPreferences",
    "applySettingsPreferenceDraft", "hydrateSettingsPreferences", "reconcileSettingsSave", "settingsActivationWarningText",
)


COMMON = r"""
const assert=require('node:assert/strict');
for(const mod of ['node:net','node:http','node:https']){
  const m=require(mod);for(const k of ['connect','createConnection','request','get'])
    if(typeof m[k]==='function')m[k]=()=>{throw Error('Network forbidden');};
}
class HTMLInputElement {
  constructor(selector){
    this.id=selector.slice(1);this.value='';this.checked=false;this.disabled=false;
    this.dataset={};this.textContent='';this.innerHTML='';this.className='';
    this.type='text';this.tagName='INPUT';this.options=[];this.scrollTop=0;this.open=true;
    this.attrs=new Map();const classes=new Set();
    this.classList={add:n=>classes.add(n),remove:n=>classes.delete(n),contains:n=>classes.has(n),
      toggle:(n,v)=>{if(v??!classes.has(n))classes.add(n);else classes.delete(n);}};
  }
  closest(){return null;} querySelector(){return null;} querySelectorAll(){return [];}
  addEventListener(){}
  setAttribute(k,v){this.attrs.set(k,v);} removeAttribute(k){this.attrs.delete(k);}
  toggleAttribute(k,v){if(v)this.attrs.set(k,'');else this.attrs.delete(k);}
  insertAdjacentHTML(where,html){this.innerHTML+=html;} close(){this.open=false;}
}
const elements=new Map(),$=s=>{if(!elements.has(s))elements.set(s,new HTMLInputElement(s));return elements.get(s);};
const noop=()=>{},notices=[],timers=[],requests=[];
const setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout=noop;
const api=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
const uiText=(zh,en)=>en,isEnglish=()=>true,escapeHtml=s=>String(s??''),localizeKnownSystemMessage=s=>s;
const showToast=(...args)=>notices.push(args),showWorkspaceToast=showToast;
const renderAttachments=noop,resizePromptInput=noop,syncComposerAction=noop;
const document={querySelectorAll:()=>[]},window={dispatchEvent:noop},CustomEvent=function(){};
let taskViewGeneration=1,uploadRequestPending=false,composerDraftScope='a',pendingAttachments=[];
let currentTaskSnapshot={id:'a'},pendingLanguageSwitchModel='';
const composerDrafts=new Map();
const tick=()=>Promise.resolve();
"""


def run_js(body, names, setup=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    controls = re.findall(r'<(?:input|select|textarea)\b[^>]*\bid="(setting[^"]+)"', INDEX)
    dom_setup = "\n" + json.dumps(controls) + ".forEach(id=>$('#'+id));\n"
    deps = SETTINGS_REPAIR_HELPERS if "saveSettings" in names or "loadSettings" in names else ()
    script = COMMON + dom_setup + setup + "\n" + "\n".join(function(n) for n in dict.fromkeys((*deps, *names)))
    if deps:
        script += "\nsettingsPreferenceBaseline=settingsPreferenceValues();\n"
    script += "\n(async()=>{\n" + body + "\nconsole.log('passed');})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                            encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"


UPLOAD = ("uploadFiles", "switchComposerDraft")


@pytest.mark.parametrize("origin", ["a", None])
@pytest.mark.parametrize("return_before", [False, True])
def test_upload_navigation_retains_all_origin_files_and_both_drafts(origin, return_before):
    run_js("const origin=" + json.dumps(origin) + ";const returnBefore=" + json.dumps(return_before) + ";" + r"""
      composerDraftScope=origin;$('#prompt').value='origin draft';
      pendingAttachments=[{path:'existing',name:'existing'}];
      const files=['one','two'].map(name=>({name,type:'text/plain',arrayBuffer:async()=>new ArrayBuffer(0)}));
      const p=uploadFiles(files);await tick();assert.equal(requests.length,1);
      switchComposerDraft('b');taskViewGeneration++;$('#prompt').value='B newer draft';
      pendingAttachments=[{path:'b',name:'B attachment'}];
      if(returnBefore){switchComposerDraft(origin);taskViewGeneration++;$('#prompt').value='origin newer typing';}
      requests[0].resolve({name:'one',path:'one'});await tick();await tick();
      assert.equal(requests.length,2,'Navigation must not silently cancel later selected files');
      requests[1].resolve({name:'two',path:'two'});await p;
      if(!returnBefore){
        assert.equal($('#prompt').value,'B newer draft');assert.deepEqual(pendingAttachments.map(a=>a.path),['b']);
        switchComposerDraft(origin);taskViewGeneration++;
      }
      assert.equal($('#prompt').value,returnBefore?'origin newer typing':'origin draft');
      assert.deepEqual(pendingAttachments.map(a=>a.path),['existing','one','two']);
      switchComposerDraft('b');assert.equal($('#prompt').value,'B newer draft');
      assert.deepEqual(pendingAttachments.map(a=>a.path),['b']);
      assert.equal(uploadRequestPending,false);assert.equal($('#uploadFile').disabled,false);
      assert(notices[0][0].includes('originating chat draft'));
    """, UPLOAD)


@pytest.mark.parametrize("fail_at", [0, 1])
def test_upload_partial_failure_is_visible_and_retains_completed_origin_files(fail_at):
    run_js("const failAt=" + str(fail_at) + ";" + r"""
      const p=uploadFiles(['one','two'].map(name=>({name,arrayBuffer:async()=>new ArrayBuffer(0)})));
      await tick();switchComposerDraft('b');taskViewGeneration++;
      if(failAt){requests[0].resolve({path:'one'});await tick();await tick();}
      requests[failAt].reject(Error('synthetic failure'));await p;
      assert.equal(pendingAttachments.length,0);assert.equal(notices.length,1);
      assert.equal(notices[0][1],'error');assert(notices[0][0].includes('originating chat'));
      switchComposerDraft('a');assert.equal(pendingAttachments.length,failAt);
      assert.equal(uploadRequestPending,false);
    """, UPLOAD)


def test_upload_limits_and_duplicate_handler_guard_remain_enforced():
    run_js(r"""
      pendingAttachments=Array.from({length:19},(_,i)=>({path:'old'+i}));
      const files=['one','two'].map(name=>({name,arrayBuffer:async()=>new ArrayBuffer(0)}));
      const p=uploadFiles(files);await tick();await uploadFiles(files);
      assert.equal(requests.length,1);requests[0].resolve({path:'one'});await p;
      assert.equal(pendingAttachments.length,20);await uploadFiles(files);
      assert.equal(requests.length,1);assert.equal(notices.at(-1)[1],'error');
    """, UPLOAD)


HISTORY = ("loadTaskHistory", "historyResultSignature")
HISTORY_SETUP = r"""
let historyLoading=false,historyReloadPending=false,historyReloadWaiters=[],historyAppendPending=false;
let historyOffset=0,historyHasMore=false,historyFirstPageSignature='',knownHistoryTaskIds=new Set(),historyLoadedFilter=null;
const HISTORY_PAGE_SIZE=50,openTask=noop,openTaskContextMenu=noop;
const historyMarkup=tasks=>tasks.map(t=>'['+t.id+']').join('');
const tasks=(start,count)=>Array.from({length:count},(_,i)=>({id:'task-'+(start+i),status:'completed'}));
async function firstPage(){
  const p=loadTaskHistory();requests.at(-1).resolve({tasks:tasks(1,50),total:100,has_more:true});await p;
}
"""


@pytest.mark.parametrize("background", [False, True])
def test_history_refresh_failure_keeps_list_cursor_and_subsequent_append(background):
    run_js("const background=" + json.dumps(background) + ";" + r"""
      await firstPage();const html=$('#taskHistory').innerHTML;
      const p=loadTaskHistory({background});requests.at(-1).reject(Error('offline'));await p;
      assert.equal($('#taskHistory').innerHTML,html);assert.equal(historyOffset,50);
      assert.equal(knownHistoryTaskIds.size,50);assert.equal(historyHasMore,true);
      const next=loadTaskHistory({append:true});assert(requests.at(-1).url.includes('offset=50'));
      requests.at(-1).resolve({tasks:tasks(51,50),total:100,has_more:false});await next;
      assert($('#taskHistory').innerHTML.includes('[task-1]'));assert($('#taskHistory').innerHTML.includes('[task-100]'));
      assert(!$('#taskHistory').innerHTML.includes('Failed'));assert.equal(historyOffset,100);
      assert.equal(historyHasMore,false);assert.equal(notices.length,background?0:1);
    """, HISTORY, HISTORY_SETUP)


@pytest.mark.parametrize("filter_field", ["#historySearch", "#historyStatus"])
def test_failed_changed_filter_resets_state_and_direct_more_restarts_first_page(filter_field):
    run_js("const filter=" + json.dumps(filter_field) + ";" + r"""
      await firstPage();$(filter).value='changed';
      const p=loadTaskHistory();requests.at(-1).reject(Error('offline'));await p;
      assert.equal(historyOffset,0);assert.equal(historyLoadedFilter,null);
      assert.equal(knownHistoryTaskIds.size,0);assert.equal(historyHasMore,false);
      assert.equal($('#historyMore').classList.contains('hidden'),true);
      assert(!$('#taskHistory').innerHTML.includes('[task-1]'));
      const retry=loadTaskHistory({append:true});assert(requests.at(-1).url.includes('offset=0'));
      requests.at(-1).resolve({tasks:tasks(90,2),total:2,has_more:false});await retry;
      assert.equal($('#taskHistory').innerHTML,'[task-90][task-91]');assert.equal(historyOffset,2);
    """, HISTORY, HISTORY_SETUP)


@pytest.mark.parametrize("failure", [False, True])
def test_old_filter_success_or_failure_cannot_replace_new_filter_and_queued_refresh_finishes(failure):
    run_js("const failure=" + json.dumps(failure) + ";" + r"""
      await firstPage();const p=loadTaskHistory({append:true});$('#historySearch').value='new';
      let finished=false;const q=loadTaskHistory().then(()=>finished=true);
      if(failure)requests.at(-1).reject(Error('old error'));
      else requests.at(-1).resolve({tasks:tasks(51,50),total:100,has_more:false});
      await p;await tick();assert.equal(finished,false);
      assert(requests.at(-1).url.includes('query=new'));assert(requests.at(-1).url.includes('offset=0'));
      requests.at(-1).resolve({tasks:tasks(90,1),total:1,has_more:false});await q;
      assert.equal($('#taskHistory').innerHTML,'[task-90]');assert.equal(historyOffset,1);
      assert.equal(historyLoading,false);assert.equal(notices.length,0);
    """, HISTORY, HISTORY_SETUP)


def test_append_failure_keeps_page_one_and_retries_same_cursor():
    run_js(r"""
      await firstPage();const p=loadTaskHistory({append:true});requests.at(-1).reject(Error('append error'));await p;
      assert.equal(historyOffset,50);assert.equal(knownHistoryTaskIds.size,50);
      const retry=loadTaskHistory({append:true});assert(requests.at(-1).url.includes('offset=50'));
      requests.at(-1).resolve({tasks:tasks(50,2),total:51,has_more:false});await retry;
      assert.equal(($('#taskHistory').innerHTML.match(/\[task-50\]/g)||[]).length,1);
      assert($('#taskHistory').innerHTML.includes('[task-51]'));
    """, HISTORY, HISTORY_SETUP)


def test_queued_more_survives_background_failure_without_losing_first_page():
    run_js(r"""
      await firstPage();const p=loadTaskHistory({background:true});const next=loadTaskHistory({append:true});
      assert.equal(requests.length,2);assert.equal($('#historyMore').disabled,true);
      requests.at(-1).reject(Error('background failure'));await p;await tick();
      assert.equal(requests.length,3);assert(requests.at(-1).url.includes('offset=50'));
      requests.at(-1).resolve({tasks:tasks(51,50),total:100,has_more:false});await next;
      assert($('#taskHistory').innerHTML.includes('[task-1]'));assert($('#taskHistory').innerHTML.includes('[task-100]'));
      assert.equal(historyOffset,100);assert.equal(historyAppendPending,false);
      assert.equal($('#historyMore').disabled,false);
    """, HISTORY, HISTORY_SETUP)


def test_initial_failure_never_exposes_more_and_recovery_installs_page_one():
    run_js(r"""
      const p=loadTaskHistory();requests.at(-1).reject(Error('first request failed'));await p;
      assert.equal(historyLoadedFilter,null);assert.equal(historyOffset,0);
      assert.equal($('#historyMore').classList.contains('hidden'),true);
      const retry=loadTaskHistory({append:true});assert(requests.at(-1).url.includes('offset=0'));
      requests.at(-1).resolve({tasks:tasks(1,50),total:100,has_more:true});await retry;
      assert.equal(historyOffset,50);assert(!$('#taskHistory').innerHTML.includes('Failed'));
      assert.equal($('#historyMore').classList.contains('hidden'),false);
    """, HISTORY, HISTORY_SETUP)


SETTINGS = (
    "providerCredentialFields", "clearProviderCredentialDrafts", "requestCloseWorkspaceDialog",
    "discardSettingsChanges", "setSettingsFormHydrating", "setSettingsFormDirty", "loadSettings",
    "setProviderKeyPlaceholders", "settingsFormSignature", "rememberSettingsFormBaseline",
    "showSettingsCleanHint", "showRecoveredDiscussionTeamDraftHint", "saveSettings",
)
SETTINGS_SETUP = r"""
let settingsFormDirty=true,settingsFormHydrating=false,settingsFormBaseline=null;
let settingsRequestGeneration=0,settingModelDirty=false,settingReasoningDirty=false;
let settingsRetryTimer=null,settingsRetryDelay=1000,settingsApiVersion=2;
let settingsSnapshotPromise=null,settingsSnapshotStartedAt=0,settingsSavePending=false;
let settingsPreferenceBaseline=null;
let discussionTeamDirty=false,recoveredDiscussionTeamDraftPending=false,confirmDiscard=true;
let latestStatus={primary_key_configured:true};
const savedSettings={settings_api_version:2,model:'synthetic-model',reasoning_effort:'high',discussion_team:[],request_timeout:120};
let requestSettingsSnapshot=()=>{settingsSnapshotPromise=Promise.resolve({...savedSettings});return settingsSnapshotPromise;};
const cacheSettingsDisplay=noop,renderModelProviderRows=noop,updateOutputTokenHelp=noop;
const syncModelSelectors=s=>{$('#settingModel').value=s.model;$('#settingReasoningEffort').value=s.reasoning_effort;};
const syncReasoningAvailability=noop,populateSettingVoiceNames=noop,readDiscussionTeamDraft=()=>null,discussionTeamDraftDiffers=()=>false;
const clearDiscussionTeamDraft=noop,renderDiscussionTeamRows=noop,enhanceAllSettingsSelects=noop;
const syncDefaultReasoningOptions=noop,persistDiscussionTeamDraft=noop;
const collectDiscussionTeamRows=()=>[],collectModelProviderRows=()=>[],canonicalDiscussionTeam=x=>x;
const loadStatus=async()=>{},loadMobileDevices=async()=>{},renderVisionSettings=noop,translateSettingsDynamic=noop;
const modelDisplayName=s=>s,openClawStatusText=()=>'';
const askForConfirmation=async()=>confirmDiscard,setPrimaryNavigation=noop,focusMainContentTarget=noop;
$('#settingsForm').querySelectorAll=()=>[...elements.values()].filter(e=>e.id.startsWith('setting')&&!['settingsForm','settingsSaveState'].includes(e.id));
"""


def test_credential_inventory_covers_every_placeholder_and_has_unique_payload_fields():
    fields = re.findall(r'\["(#setting[^"\n]+)", "([a-z_]+)"\]', function("providerCredentialFields"))
    placeholders = re.findall(r'\["(#setting[^"\n]+)", settings\.', function("setProviderKeyPlaceholders"))
    assert len(fields) == 31
    assert {selector for selector, _ in fields} == set(placeholders)
    assert len({field for _, field in fields}) == len(fields)
    index = (ROOT / "deepdesk/static/index.html").read_text("utf-8")
    for selector, _ in fields:
        assert f'id="{selector[1:]}"' in index


@pytest.mark.parametrize("reload_fails", [False, True])
def test_discard_clears_all_supported_fields_before_reload_and_never_resubmits_them(reload_fails):
    run_js("const reloadFails=" + json.dumps(reload_fails) + ";" + r"""
      const fields=providerCredentialFields();fields.forEach(([s])=>$(s).value='SYNTHETIC-NOT-A-KEY');
      assert.equal(await requestCloseWorkspaceDialog(),true);
      for(const [s] of fields)assert.equal($(s).value,'','discard clears immediately, including load failure');
      if(reloadFails)requestSettingsSnapshot=async()=>{throw Error('synthetic load failure');};
      await loadSettings();assert.equal(timers.length,reloadFails?1:0);
      if(reloadFails){requestSettingsSnapshot=()=>{settingsSnapshotPromise=Promise.resolve({...savedSettings});return settingsSnapshotPromise;};await loadSettings();}
      assert.equal(settingsFormDirty,false);$('#settingTimeout').value='180';
      const p=saveSettings({preventDefault(){}});assert.equal(requests.length,1);
      const body=JSON.parse(requests[0].options.body);
      assert.equal(body.request_timeout,180);
      for(const [,field] of fields)assert.equal(Object.hasOwn(body,field),false,field);
      requests[0].resolve(savedSettings);await p;assert.equal(settingsSavePending,false);
    """, SETTINGS, SETTINGS_SETUP)


def test_declining_discard_keeps_all_fields_and_explicit_save_submits_them():
    run_js(r"""
      const fields=providerCredentialFields();fields.forEach(([s])=>$(s).value='SYNTHETIC-NOT-A-KEY');
      confirmDiscard=false;assert.equal(await requestCloseWorkspaceDialog(),false);
      assert.equal($('#workspaceDialog').open,true);assert.equal(settingsFormDirty,true);
      for(const [s] of fields)assert.equal($(s).value,'SYNTHETIC-NOT-A-KEY');
      const p=saveSettings({preventDefault(){}});assert.equal(requests.length,1);
      const body=JSON.parse(requests[0].options.body);
      for(const [,field] of fields)assert.equal(body[field],'SYNTHETIC-NOT-A-KEY');
      requests[0].resolve(savedSettings);await p;
      for(const [s] of fields)assert.equal($(s).value,'');
    """, SETTINGS, SETTINGS_SETUP)


def test_loading_existing_configuration_clears_all_write_only_fields():
    run_js(r"""
      settingsFormDirty=false;providerCredentialFields().forEach(([s])=>$(s).value='SYNTHETIC-AUTOFILL');
      await loadSettings();assert.equal(timers.length,0);
      for(const [s] of providerCredentialFields())assert.equal($(s).value,'');
      assert.equal(settingsFormDirty,false);assert.equal(settingsFormHydrating,false);
    """, SETTINGS, SETTINGS_SETUP)


@pytest.mark.parametrize("newer_draft", [False, True])
@pytest.mark.parametrize("component, label", [("telegram", "Telegram"), ("feishu", "Feishu"),
                                              ("openclaw", "OpenClaw"),
                                              ("vision", "Vision")])
def test_saved_with_activation_warning_is_not_silent_success_or_failed_save(newer_draft, component, label):
    run_js("const newer=" + json.dumps(newer_draft) + ";const component=" + json.dumps(component)
           + ";const label=" + json.dumps(label) + ";" + r"""
      $('#settingGithubToken').value='SYNTHETIC-SUBMITTED';
      const p=saveSettings({preventDefault(){}});assert.equal(requests.length,1);
      if(newer)$('#settingGithubToken').value='SYNTHETIC-NEWER';
      requests[0].resolve({...savedSettings,saved:true,activation_warnings:[{component,code:'activation_failed'}]});await p;
      assert.equal(settingsFormDirty,newer);assert.equal(settingsSavePending,false);
      assert.equal($('#settingGithubToken').value,newer?'SYNTHETIC-NEWER':'');
      assert.equal(notices.length,1);assert.equal(notices[0][1],'info');
      assert(notices[0][0].startsWith('Settings saved, but'));assert(notices[0][0].includes(label));
      assert(!notices[0][0].includes('Saved and applied'));assert(!notices[0][0].includes('Save failed'));
      assert.equal($('#settingsSaveState').textContent,notices[0][0]);
      assert.equal($('#settingsSaveState').className.includes('warning'),true);
      assert.equal(notices[0][0].includes('Newer changes are still unsaved'),newer);
    """, SETTINGS, SETTINGS_SETUP)
