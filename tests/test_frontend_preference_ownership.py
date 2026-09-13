"""Committed preferences, manual choices, stale reads and task-route ownership."""

import json
import re
import shutil
import subprocess

import pytest
from test_frontend_omission_repairs import INDEX, SETTINGS_REPAIR_HELPERS, SOURCE, function

PRELUDE = r"""
const assert=require('node:assert/strict'),noop=()=>{};
for(const mod of ['node:net','node:http','node:https']){
  const m=require(mod);for(const k of ['connect','createConnection','request','get'])
    if(typeof m[k]==='function')m[k]=()=>{throw Error('Network forbidden');};
}
class HTMLInputElement {
  constructor(id){
    this.id=id;this._value='';this._html='';this.options=[];this.dataset={};this.textContent='';
    this.disabled=false;this.hidden=false;this.checked=false;this.open=true;this.className='';
    this.listeners=new Map();this.style={setProperty:noop};this.attrs=new Map();
    this.tagName=['modelPreference','settingModel','settingReasoningEffort'].includes(id)?'SELECT':'INPUT';
    this.classList={toggle:noop,add:noop,remove:noop,contains:()=>false};
  }
  set innerHTML(html){this._html=html;if(this.tagName==='SELECT'){
    this.options=[...html.matchAll(/<option value="([^"]*)"[^>]*>(.*?)<\/option>/g)].map(m=>({value:m[1],textContent:m[2]}));
    this._value=this.options[0]?.value||'';}}
  get innerHTML(){return this._html;}
  set value(value){value=String(value);this._value=this.tagName==='SELECT'&&!this.options.some(o=>o.value===value)?'':value;}
  get value(){return this._value;}
  closest(s){if(s==='.reasoning-picker')return picker;if(s==='.reasoning-setting')return {querySelector:()=>help};return null;}
  setAttribute(k,v){this.attrs.set(k,v);}removeAttribute(k){this.attrs.delete(k);}
  toggleAttribute(k,v){if(v)this.attrs.set(k,'');else this.attrs.delete(k);}
  addEventListener(type,fn){this.listeners.set(type,fn);}querySelector(){return null;}querySelectorAll(){return [];}
  insertAdjacentHTML(where,html){this.innerHTML=this.innerHTML+html;}close(){this.open=false;}
}
const elements=new Map(),picker=new HTMLInputElement('picker'),help=new HTMLInputElement('help');
const $=s=>{if(!elements.has(s))elements.set(s,new HTMLInputElement(s.slice(1)));return elements.get(s);};
const localStore=new Map(),sessionStore=new Map();
const storage=map=>({getItem:k=>map.has(k)?map.get(k):null,setItem:(k,v)=>map.set(k,String(v)),removeItem:k=>map.delete(k)});
const window={location:{href:'http://synthetic.invalid/?lang=en&task=A&ui_release=qa#details'},
  localStorage:storage(localStore),sessionStorage:storage(sessionStore),
  history:{replaceState:(_a,_b,url)=>{window.location.href=url;}},dispatchEvent:noop};
const document={querySelector:()=>null,querySelectorAll:()=>[]};
let now=1;Date.now=()=>now;
const requests=[],api=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
const notices=[],showWorkspaceToast=(...args)=>notices.push(args),showToast=showWorkspaceToast;
const timers=[],setTimeout=fn=>{timers.push(fn);return timers.length;},clearTimeout=noop;
const SETTINGS_DISPLAY_STORAGE_KEY='settings',SETTINGS_SNAPSHOT_REUSE_MS=15000,RUNTIME_STATUS_STORAGE_KEY='status';
let settingsSnapshotPromise=null,settingsSnapshotStartedAt=0,settingsRequestGeneration=0;
let settingsSavePending=false,settingsApiVersion=2,settingsFormBaseline=null,settingsFormHydrating=false;
let settingsPreferenceBaseline=null;
let settingsRetryTimer=null,settingsRetryDelay=1000,discussionTeamDirty=false,recoveredDiscussionTeamDraftPending=false;
let settingModelDirty=false,settingReasoningDirty=false,settingsFormDirty=false,pendingLanguageSwitchModel='';
let defaultModelSelector='auto',automaticDefaultModelSelector='',newTaskModelPreference='auto',newTaskReasoningPreference='auto';
let availableModelOptions=[],discussionTeamConfigured=false,discussionTeamLeaderModel='auto';
let activeReasoningLevels=['auto'],reasoningDefaultLoaded=false,latestStatus=null;
let taskId=null,continuationTaskId=null,currentTaskSnapshot=null,startRequestPending=false;
const ALL_REASONING_LEVELS=['auto','minimal','low','medium','high','xhigh','max'];
const uiText=(zh,en)=>en,isEnglish=()=>true,escapeHtml=s=>String(s??''),localizeKnownSystemMessage=s=>s;
const refreshDiscussionTeamModelOptions=noop,refreshModelPreferenceUI=noop,updateReasoningVisualState=noop;
const renderVisionSettings=noop,renderMobileDevices=noop,translateSettingsDynamic=noop,openClawStatusText=()=>'';
const updateOutputTokenHelp=noop,renderModelProviderRows=noop,clearDiscussionTeamDraft=noop;
const collectDiscussionTeamRows=()=>[],collectModelProviderRows=()=>[],canonicalDiscussionTeam=x=>x;
const populateSettingVoiceNames=noop,readDiscussionTeamDraft=()=>null,discussionTeamDraftDiffers=()=>false;
const renderDiscussionTeamRows=noop,enhanceAllSettingsSelects=noop,loadStatus=async()=>{},loadMobileDevices=async()=>{};
const persistDiscussionTeamDraft=noop;
const CustomEvent=function(){};
const capabilities=[
  {selector:'model-a',model:'synthetic-a',reasoning:{supported:true,levels:['high','max'],control:'synthetic'}},
  {selector:'model-b',model:'synthetic-b',reasoning:{supported:true,levels:['low','medium','high','max'],control:'synthetic'}},
  {selector:'unknown',model:'synthetic-unknown',reasoning:{supported:false,levels:[],control:'none'}},
];
const settings=(model='model-a',reasoning='high')=>({model,active_model:model,reasoning_effort:reasoning,
  available_models:capabilities,settings_api_version:2,discussion_team:[],request_timeout:120});
const status=(model='model-a',reasoning='high')=>({...settings(model,reasoning),default_model:model,primary_key_configured:true});
$('#modelPreference').innerHTML='<option value="auto">Auto</option>';
$('#settingModel').innerHTML='<option value="auto">Auto</option>';
$('#settingsForm').querySelectorAll=()=>[...elements.values()].filter(e=>e.id.startsWith('setting')&&!['settingsForm','settingsSaveState'].includes(e.id));
const tick=async()=>{for(let i=0;i<5;i++)await Promise.resolve();};
"""

MODELS = (
    "isRetiredBuiltinModel",
    "modelDisplayName", "modelOptionsForDisplay", "reasoningLevelLabel", "reasoningPreferenceValue",
    "effectiveModelSelector", "reasoningCapability", "syncReasoningDots", "setReasoningPreference",
    "syncReasoningAvailability", "syncDefaultReasoningOptions", "syncModelSelectors", "isBlankNewTaskComposer",
    "rememberNewTaskComposerPreferences", "renderRuntimeStatus", "cacheRuntimeStatus", "cacheSettingsDisplay",
)
READS = ("requestSettingsSnapshot", "preloadSettingsDisplay")
SETTINGS = (
    "saveSettings", "providerCredentialFields", "clearProviderCredentialDrafts", "loadSettings",
    "setSettingsFormHydrating", "setSettingsFormDirty", "setProviderKeyPlaceholders",
    "settingsFormSignature", "rememberSettingsFormBaseline", "showSettingsCleanHint", "showRecoveredDiscussionTeamDraftHint",
)


def handler(selector, event):
    regex = rf'^\$\("{re.escape(selector)}"\)\.addEventListener\("{event}", \(\) => \{{[\s\S]*?^}}\);'
    match = re.search(regex, SOURCE, re.MULTILINE)
    assert match
    return match.group()


def run_js(body, *, names=MODELS, setup="", handlers=()):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    controls = re.findall(r'<(?:input|select|textarea)\b[^>]*\bid="(setting[^"]+)"', INDEX)
    script = PRELUDE + "\n" + json.dumps(controls) + ".forEach(id=>$('#'+id));\n" + setup + "\n"
    deps = SETTINGS_REPAIR_HELPERS if "saveSettings" in names or "loadSettings" in names else ()
    script += "\n".join(function(n) for n in dict.fromkeys((*deps, *names))) + "\n" + "\n".join(handler(*h) for h in handlers)
    if deps:
        script += "\nsettingsPreferenceBaseline=settingsPreferenceValues();\n"
    script += "\n(async()=>{\n" + body + "\nconsole.log('passed');})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                            encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"


@pytest.mark.parametrize("first", ["settings", "status"])
@pytest.mark.parametrize("choice", ["auto", "medium", "max"])
def test_first_default_initializes_untouched_only_and_late_status_respects_actual_input(first, choice):
    run_js("const first=" + json.dumps(first) + ";const choice=" + json.dumps(choice) + ";" + r"""
      if(first==='settings')syncModelSelectors(settings('model-b','max'));
      else renderRuntimeStatus(status('model-b','max'));
      assert.equal(reasoningPreferenceValue(),'max');assert.equal(reasoningDefaultLoaded,true);
      setReasoningPreference(choice);$('#reasoningPreference').listeners.get('input')();
      renderRuntimeStatus(status('model-b','high'));
      assert.equal(reasoningPreferenceValue(),choice);assert.equal(newTaskReasoningPreference,choice);
      assert.equal(effectiveModelSelector(),'model-b');assert.equal(requests.length,0);
    """, handlers=(("#reasoningPreference", "input"),))


def test_removed_model_cannot_return_from_old_backend_catalog_or_dirty_default():
    run_js(r'''
      const removed='deepseek-v4.1-flash-expires-on-0910';
      $('#settingModel').innerHTML='<option value="'+removed+'">Old</option>';
      $('#settingModel').value=removed;settingModelDirty=true;
      const snapshot=settings(removed,'max');
      snapshot.available_models=[...capabilities,{selector:removed,model:removed}];
      syncModelSelectors(snapshot);
      assert.equal($('#settingModel').value,'auto');
      assert.equal(defaultModelSelector,'auto');
      assert(!availableModelOptions.some(x=>x.selector===removed));
      assert(!$('#modelPreference').innerHTML.includes(removed));
      assert(!$('#settingModel').innerHTML.includes(removed));
      assert.equal(snapshot.model,removed); // do not mutate the network snapshot
    ''')


def test_removed_model_display_cache_does_not_flash_old_label_or_reasoning():
    run_js(r'''
      window.localStorage.setItem(SETTINGS_DISPLAY_STORAGE_KEY,JSON.stringify({
        savedAt:Date.now(),model:'deepseek-v4.1-flash-expires-on-0910',modelLabel:'Retired',
        reasoningSupported:true,reasoningLevels:['high','max'],reasoningEffort:'max'
      }));
      hydrateSettingsDisplayFromCache();
      assert.equal($('#settingModel').value,'auto');
      assert.equal($('#settingReasoningEffort').value,'auto');
      assert.equal($('#settingReasoningEffort').disabled,true);
      assert(!$('#settingModel').options.some(o=>o.textContent==='Retired'));
    ''',names=(*MODELS,'hydrateSettingsDisplayFromCache'),setup='const SETTINGS_DISPLAY_CACHE_TTL_MS=60000;')


def test_user_input_before_any_default_snapshot_is_not_reinitialized():
    run_js(r"""
      const initial=settings('model-b');delete initial.reasoning_effort;syncModelSelectors(initial);
      assert.equal(reasoningDefaultLoaded,false);setReasoningPreference('medium');
      $('#reasoningPreference').listeners.get('input')();assert.equal(reasoningDefaultLoaded,true);
      renderRuntimeStatus(status('model-b','max'));assert.equal(reasoningPreferenceValue(),'medium');
    """, handlers=(("#reasoningPreference", "input"),))


def test_real_settings_load_after_manual_reasoning_input_preserves_choice():
    run_js(r"""
      syncModelSelectors(settings('model-b','high'));setReasoningPreference('medium');
      $('#reasoningPreference').listeners.get('input')();
      const p=loadSettings();requests.at(-1).resolve(settings('model-b','max'));await p;
      assert.equal(timers.length,0);assert.equal(reasoningPreferenceValue(),'medium');
      assert.equal($('#settingReasoningEffort').value,'max');
    """, names=(*MODELS, *READS, *SETTINGS), handlers=(("#reasoningPreference", "input"),))


@pytest.mark.parametrize("draft", ["model-b", "auto", "unknown"])
def test_unsaved_default_draft_is_preserved_but_never_activates_auto_capability(draft):
    run_js("const draft=" + json.dumps(draft) + ";" + r"""
      renderRuntimeStatus(status('model-a','high'));$('#settingModel').value=draft;
      $('#settingModel').listeners.get('change')();settingsFormDirty=true;
      for(let i=0;i<3;i++)renderRuntimeStatus(status('model-a','high'));
      assert.equal($('#settingModel').value,draft);assert.equal($('#modelPreference').value,'auto');
      assert.equal(defaultModelSelector,'model-a');assert.equal(effectiveModelSelector(),'model-a');
      assert.deepEqual(activeReasoningLevels,['auto','high','max']);assert.equal(requests.length,0);
    """, handlers=(("#settingModel", "change"),))


def test_acknowledged_model_save_changes_auto_capability_not_before_ack():
    run_js(r"""
      renderRuntimeStatus(status('model-a','high'));$('#settingModel').value='model-b';
      $('#settingModel').listeners.get('change')();settingsFormDirty=true;
      const p=saveSettings({preventDefault(){}});assert.equal(defaultModelSelector,'model-a');
      requests.at(-1).resolve(settings('model-b','medium'));await p;
      assert.equal(defaultModelSelector,'model-b');assert.equal(effectiveModelSelector(),'model-b');
      assert(activeReasoningLevels.includes('medium'));assert.equal(settingsFormDirty,false);
    """, names=(*MODELS, *READS, *SETTINGS), handlers=(("#settingModel", "change"),))


def test_unsupported_capability_still_disables_a_previously_valid_manual_choice():
    run_js(r"""
      renderRuntimeStatus(status('model-b','high'));setReasoningPreference('medium');
      $('#reasoningPreference').listeners.get('input')();$('#modelPreference').value='unknown';
      syncReasoningAvailability();assert.equal(reasoningPreferenceValue(),'auto');
      assert.equal(picker.hidden,true);assert.equal($('#reasoningPreference').disabled,true);
    """, handlers=(("#reasoningPreference", "input"),))


def test_historical_task_projection_and_status_do_not_replace_new_task_model_or_reasoning():
    run_js(r"""
      renderRuntimeStatus(status('model-b','high'));$('#modelPreference').value='model-b';
      setReasoningPreference('medium');$('#reasoningPreference').listeners.get('input')();
      assert.equal(newTaskModelPreference,'model-b');assert.equal(newTaskReasoningPreference,'medium');
      currentTaskSnapshot={id:'history-a',status:'completed'};continuationTaskId='history-a';
      $('#modelPreference').value='model-a';syncReasoningAvailability('model-a','max');
      renderRuntimeStatus(status('model-b','high'));
      assert.equal($('#modelPreference').value,'model-a');assert.equal(reasoningPreferenceValue(),'max');
      assert.equal(newTaskModelPreference,'model-b');assert.equal(newTaskReasoningPreference,'medium');
      currentTaskSnapshot=null;continuationTaskId=null;restoreNewTaskComposerPreferences();
      assert.equal($('#modelPreference').value,'model-b');assert.equal(reasoningPreferenceValue(),'medium');
    """, names=(*MODELS, "restoreNewTaskComposerPreferences"), handlers=(("#reasoningPreference", "input"),))


@pytest.mark.parametrize("stale_failure", [False, True])
def test_stale_prefetch_after_newer_settings_and_save_cannot_write_cache_or_selector(stale_failure):
    run_js("const staleFailure=" + json.dumps(stale_failure) + ";" + r"""
      preloadSettingsDisplay();now=15002;const fresh=requestSettingsSnapshot();
      requests[1].resolve(settings('model-a','high'));syncModelSelectors(await fresh);
      $('#settingModel').value='model-b';settingModelDirty=true;settingsFormDirty=true;
      const p=saveSettings({preventDefault(){}});requests[2].resolve(settings('model-b','max'));await p;
      const cache=localStore.get(SETTINGS_DISPLAY_STORAGE_KEY);
      if(staleFailure)requests[0].reject(Error('old offline'));
      else requests[0].resolve(settings('model-a','high'));
      await tick();assert.equal(localStore.get(SETTINGS_DISPLAY_STORAGE_KEY),cache);
      assert.equal(defaultModelSelector,'model-b');assert.equal($('#settingModel').value,'model-b');
      assert.equal(settingsFormDirty,false);assert.equal((await settingsSnapshotPromise).model,'model-b');
    """, names=(*MODELS, *READS, *SETTINGS))


def test_old_prefetch_cannot_write_while_new_save_is_pending_even_if_that_save_fails():
    run_js(r"""
      syncModelSelectors(settings('model-a'));preloadSettingsDisplay();
      $('#settingModel').value='model-b';settingModelDirty=true;settingsFormDirty=true;
      const p=saveSettings({preventDefault(){}});requests[0].resolve(settings('model-a'));
      await tick();assert.equal(localStore.size,0);assert.equal($('#settingModel').value,'model-b');
      requests[1].reject(Error('synthetic failure'));await p;
      assert.equal(settingsFormDirty,true);assert.equal($('#settingModel').value,'model-b');
      const fresh=requestSettingsSnapshot();assert.equal(requests.length,3);
      requests[2].resolve(settings('model-a'));await fresh;
    """, names=(*MODELS, *READS, *SETTINGS))


def test_old_settings_load_cannot_cache_when_newer_prefetch_owns_the_snapshot():
    run_js(r"""
      latestStatus=status();const p=loadSettings();now=15002;preloadSettingsDisplay();
      requests[1].resolve({...settings('model-b'),request_timeout:300});await tick();
      const cache=localStore.get(SETTINGS_DISPLAY_STORAGE_KEY);
      requests[0].resolve(settings('model-a'));await p;
      assert.equal(localStore.get(SETTINGS_DISPLAY_STORAGE_KEY),cache);
      assert.equal($('#settingModel').value,'model-b');assert.equal(defaultModelSelector,'model-b');
      assert.equal($('#settingTimeout').value,'300');assert.notEqual(settingsFormBaseline,null);
      assert.equal(settingsFormHydrating,false);assert.equal(timers.length,0);
    """, names=(*MODELS, *READS, *SETTINGS))


def test_shared_current_prefetch_applies_and_old_failure_does_not_cancel_it():
    run_js(r"""
      preloadSettingsDisplay();now=15002;preloadSettingsDisplay();
      const current=settingsSnapshotPromise;requests[0].reject(Error('old'));
      await tick();assert.equal(settingsSnapshotPromise,current);
      requests[1].resolve(settings('model-b','max'));await tick();
      assert.equal($('#settingModel').value,'model-b');assert.equal(reasoningPreferenceValue(),'max');
      assert.equal(JSON.parse(localStore.get(SETTINGS_DISPLAY_STORAGE_KEY)).model,'model-b');
    """, names=(*MODELS, *READS))


def test_superseded_settings_load_waits_for_newer_pending_read_before_clean_baseline():
    run_js(r"""
      latestStatus=status();let settled=false;const p=loadSettings().then(()=>settled=true);
      now=15002;preloadSettingsDisplay();requests[0].resolve(settings('model-a'));
      await tick();assert.equal(settled,false);assert.equal(settingsFormBaseline,null);
      assert.equal(settingsFormHydrating,true);assert.equal(localStore.size,0);
      requests[1].resolve({...settings('model-b','max'),request_timeout:300});await p;
      assert.equal($('#settingTimeout').value,'300');assert.equal($('#settingModel').value,'model-b');
      assert.equal(settingsFormHydrating,false);assert.notEqual(settingsFormBaseline,null);
      assert.equal(timers.length,0);
    """, names=(*MODELS, *READS, *SETTINGS))


def test_programmatic_status_default_refresh_is_not_resubmitted_as_a_user_edit():
    run_js(r"""
      const read=loadSettings();requests[0].resolve(settings('model-a','high'));await read;
      renderRuntimeStatus(status('model-b','max'));
      assert.equal($('#settingModel').value,'model-b');assert.equal(settingsPreferenceBaseline.model,'model-b');
      $('#settingVoiceAutoSpeak').checked=false;settingsFormDirty=true;
      const p=saveSettings({preventDefault(){}});const patch=JSON.parse(requests[1].options.body);
      assert.deepEqual(patch,{voice_auto_speak:false});
      requests[1].resolve({...settings('model-a','high'),voice_auto_speak:false});await p;
      assert.equal($('#settingModel').value,'model-a');assert.equal(settingsFormDirty,false);
    """, names=(*MODELS, *READS, *SETTINGS))
