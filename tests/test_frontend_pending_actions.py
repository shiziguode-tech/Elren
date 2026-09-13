"""Keep late action/save responses scoped to the view and values submitted."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_frontend_omission_repairs import SETTINGS_REPAIR_HELPERS

SOURCE = (Path(__file__).resolve().parents[1] / "deepdesk/static/app.js").read_text("utf-8")


def function(name):
    match = re.search(rf"^(?:async )?function {name}\([\s\S]*?^}}", SOURCE, re.MULTILINE)
    assert match, name
    return match.group()


PRELUDE = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const $ = selector => {
  if (!elements.has(selector)) elements.set(selector, {
    id:selector.slice(1), value:'', disabled:false, checked:false, textContent:'unchanged',
    className:'unchanged', open:true, closed:false,
    classList:{hidden:false, add(name){if(name==='hidden')this.hidden=true;},remove(name){if(name==='hidden')this.hidden=false;},toggle(){},contains(){return false;}},
    close(){this.closed=true;},
  });
  return elements.get(selector);
};
const uiText = (zh,en) => en;
const isEnglish = () => true;
const localizeKnownSystemMessage = text => text;
let taskId='a', taskViewGeneration=1, taskViewLoading=false, stopRequestPending=false;
let pendingHumanAction={id:'request-a',taken_over:true}, pollTimer=1;
let currentTaskSnapshot={id:'a',status:'waiting_user',pending_human_actions:[pendingHumanAction]},surfacedHumanActionId=null;
const taskActionStates=new Map();
const document={title:'Elren'};
const taskComposerPlaceholder=()=>'',syncComposerAction=()=>{};
let resolveRequest, rejectRequest, requestCount=0;
const requests=[], notices=[], timers=[];
const api=(path, options)=>{
  requestCount++;requests.push({path,options});
  return new Promise((resolve,reject)=>{resolveRequest=resolve;rejectRequest=reject;});
};
const showToast = (...args) => notices.push(args);
const showWorkspaceToast = (...args) => notices.push(args);
const takeoverFocusMessage = () => 'focused';
const clearTimeout = () => {};
const setTimeout = callback => {timers.push(callback);return 2;};
const polls=[];
const poll=(...args)=>polls.push(args);
const window={dispatchEvent(){},focus(){}};
const CustomEvent=function(){};
let settingsSavePending=false, settingsRequestGeneration=1, settingsApiVersion=2;
let settingsSnapshotPromise=Promise.resolve({old:true}), settingsSnapshotStartedAt=1;
let settingsFormDirty=true, settingModelDirty=true, settingReasoningDirty=true;
let settingsFormBaseline='', discussionTeam=[{id:'leader'}], draftCleared=false;
let settingsPreferenceBaseline=null,settingsFormHydrating=false,discussionTeamDirty=false,recoveredDiscussionTeamDraftPending=false;
let providerRowsRendered=false;
const collectDiscussionTeamRows=()=>JSON.parse(JSON.stringify(discussionTeam));
const collectModelProviderRows=()=>[];
const cacheSettingsDisplay=()=>{};
const clearDiscussionTeamDraft=()=>{draftCleared=true;};
const syncModelSelectors=settings=>{$('#settingModel').value=settings.model;};
const renderModelProviderRows=()=>{providerRowsRendered=true;};
const setProviderKeyPlaceholders=()=>{};
const populateSettingVoiceNames=()=>{},updateOutputTokenHelp=()=>{},enhanceAllSettingsSelects=()=>{},syncDefaultReasoningOptions=()=>{};
const renderDiscussionTeamRows=entries=>{discussionTeam=JSON.parse(JSON.stringify(entries));};
const persistDiscussionTeamDraft=()=>{},clearProviderCredentialDrafts=()=>providerCredentialFields().forEach(([s])=>$(s).value='');
const signatureIds=['settingModel','settingReasoningEffort','settingTimeout','settingDeepSeekKey'];
const settingsFormSignature=()=>JSON.stringify({
  fields:signatureIds.map(key=>({key,value:$('#'+key).value})),
  discussion_team:discussionTeam,
});
const rememberSettingsFormBaseline=()=>{settingsFormBaseline=settingsFormSignature();};
const setSettingsFormDirty=dirty=>{settingsFormDirty=Boolean(dirty);};
const savedSettings={settings_api_version:2,model:'model-old',discussion_team:[{id:'leader'}]};
$('#settingModel').value='model-old';
$('#settingReasoningEffort').value='high';
$('#settingTimeout').value='120';
const submitEvent={preventDefault(){}};
"""


def run_js(body, *names):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend behavior checks")
    dependencies = (*SETTINGS_REPAIR_HELPERS, "providerCredentialFields", "taskActionKey", "updateTaskActionState", "reconcileTaskActionState", "syncTaskApproval", "syncTaskHumanAction")
    script = PRELUDE + "\n" + "\n".join(function(name) for name in dict.fromkeys((*dependencies, *names)))
    script += "\nsettingsPreferenceBaseline=settingsPreferenceValues();\n(async()=>{\n" + body + "\nconsole.log(JSON.stringify({ok:true}));\n})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True,
                            check=False, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


@pytest.mark.parametrize("method", ["takeOverHumanAction", "resolveHumanAction"])
@pytest.mark.parametrize("failure", [False, True])
def test_late_human_action_does_not_modify_other_task(method, failure):
    completion = "rejectRequest(new Error('old failure'));" if failure else "resolveRequest({request:{id:'request-a',taken_over:true},focus:{}});"
    run_js(f"""
      if('{method}'==='takeOverHumanAction')pendingHumanAction.taken_over=false;
      const request={method}(true);
      taskId='b';taskViewGeneration++;
      pendingHumanAction={{id:'request-b',taken_over:false}};
      for(const id of ['#takeOverHumanAction','#completeHumanAction','#cancelHumanAction']){{
        $(id).disabled=true;$(id).textContent='task-b';
      }}
      {completion}
      await request;
      assert.equal(pendingHumanAction.id,'request-b');
      assert.equal($('#takeOverHumanAction').textContent,'task-b');
      assert.equal($('#completeHumanAction').disabled,true);
      assert.equal($('#cancelHumanAction').disabled,true);
      assert.equal($('#humanAction').classList.hidden,false);
      assert.equal($('#humanProblemDialog').closed,false);
      assert.equal(notices.length,0);
      assert.equal(timers.length,0);
    """, method, "isCurrentHumanActionRequest", "isCurrentTaskRequest")


def test_late_takeover_does_not_replace_new_request_in_same_task():
    run_js("""
      pendingHumanAction.taken_over=false;
      const request=takeOverHumanAction();
      pendingHumanAction={id:'request-next',taken_over:false};
      currentTaskSnapshot={id:'a',status:'waiting_user',pending_human_actions:[pendingHumanAction]};
      resolveRequest({request:{id:'request-a',taken_over:true},focus:{}});
      await request;
      assert.equal(pendingHumanAction.id,'request-next');
      assert.equal(notices.length,0);
    """, "takeOverHumanAction", "isCurrentHumanActionRequest", "isCurrentTaskRequest")


def test_current_human_completion_polls_only_submitted_task():
    run_js("""
      const request=resolveHumanAction(true);
      resolveRequest({ok:true});await request;
      assert.equal(pendingHumanAction,null);
      assert.equal($('#humanAction').classList.hidden,true);
      taskId='b';taskViewGeneration++;
      timers[0]();
      assert.deepEqual(polls,[['a',1]]);
    """, "resolveHumanAction", "isCurrentHumanActionRequest", "isCurrentTaskRequest")


def test_settings_save_preserves_new_typing_model_team_and_replacement_secret():
    run_js("""
      $('#settingDeepSeekKey').value='fake-old';
      const request=saveSettings(submitEvent);
      $('#settingModel').value='model-new';
      $('#settingTimeout').value='300';
      $('#settingDeepSeekKey').value='fake-new';
      discussionTeam=[{id:'leader'},{id:'reviewer'}];
      resolveRequest(savedSettings);await request;
      assert.equal($('#settingModel').value,'model-new');
      assert.equal($('#settingTimeout').value,'300');
      assert.equal($('#settingDeepSeekKey').value,'fake-new');
      assert.equal(discussionTeam.length,2);
      assert.equal(settingsFormDirty,true);
      assert.equal(draftCleared,false);
      assert.equal(providerRowsRendered,true);
      assert.equal(settingsSavePending,false);
      assert.equal($('#saveSettings').disabled,false);
      assert.match($('#settingsSaveState').textContent,/newer changes are still unsaved/);
      assert.equal(JSON.parse(settingsFormBaseline).fields.find(f=>f.key==='settingModel').value,'model-old');
      assert.equal((await settingsSnapshotPromise).model,'model-old');
    """, "saveSettings")


def test_settings_save_clears_only_submitted_secret_and_keeps_other_edits_dirty():
    run_js("""
      $('#settingDeepSeekKey').value='fake-submitted';
      const request=saveSettings(submitEvent);
      $('#settingTimeout').value='300';
      resolveRequest(savedSettings);await request;
      assert.equal($('#settingDeepSeekKey').value,'');
      assert.equal($('#settingTimeout').value,'300');
      assert.equal(settingsFormDirty,true);
      assert.equal(JSON.parse(settingsFormBaseline).fields.find(f=>f.key==='settingDeepSeekKey').value,'');
    """, "saveSettings")


def test_settings_save_without_new_edits_acknowledges_saved_form_and_blocks_duplicate():
    run_js("""
      const request=saveSettings(submitEvent);
      await saveSettings(submitEvent);
      assert.equal(requestCount,1);
      resolveRequest(savedSettings);await request;
      assert.equal(settingsFormDirty,false);
      assert.equal(draftCleared,true);
      assert.equal(providerRowsRendered,true);
      assert.equal(settingsSavePending,false);
    """, "saveSettings")


@pytest.mark.parametrize("failure", [False, True])
def test_settings_response_does_not_write_a_new_settings_visit(failure):
    completion = "rejectRequest(new Error('old failure'));" if failure else "resolveRequest(savedSettings);"
    run_js(f"""
      const request=saveSettings(submitEvent);
      settingsRequestGeneration++;
      $('#settingModel').value='new-visit';
      $('#settingsSaveState').textContent='new-visit-state';
      settingsFormDirty=false;
      {completion}
      await request;
      assert.equal($('#settingModel').value,'new-visit');
      assert.equal($('#settingsSaveState').textContent,'new-visit-state');
      assert.equal(settingsFormDirty,false);
      assert.equal(notices.length,1);
      assert.equal(settingsSavePending,false);
    """, "saveSettings")
