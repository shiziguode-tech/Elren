"""Actual app functions with synthetic DOM and deferred I/O; never call a provider.

Approval intent, acknowledged manual actions and composer drafts must survive
polling, failed requests and navigation. UI appearance is verified separately.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parents[1] / "deepdesk/static/app.js").read_text("utf-8")


def function(name):
    match = re.search(rf"^(?:async )?function {name}\([\s\S]*?^}}", SOURCE, re.MULTILINE)
    assert match, name
    return match.group()


PRELUDE = r"""
const assert=require('node:assert/strict');
const noop=()=>{},elements=new Map(),notices=[],requests=[],timers=[],renderedEvents=[];
const $=selector=>{
  if(!elements.has(selector)){
    const classes=new Set();
    elements.set(selector,{value:'',placeholder:'',textContent:'',title:'',innerHTML:'',dataset:{},disabled:false,hidden:false,open:false,options:[],
      attrs:new Map(),classList:{add:n=>classes.add(n),remove:n=>classes.delete(n),contains:n=>classes.has(n),toggle:(n,v)=>{if(v)classes.add(n);else classes.delete(n);}},
      setAttribute(k,v){this.attrs.set(k,v);},getAttribute(k){return this.attrs.get(k);},
      insertAdjacentHTML(where,html){this.innerHTML+=html;},close(){this.open=false;},querySelector:()=>null,querySelectorAll:()=>[],focus:noop});
  }return elements.get(selector);
};
const uiText=(zh,en)=>en,isEnglish=()=>true;
const showToast=(...args)=>notices.push(args),showWorkspaceToast=showToast;
const takeoverFocusMessage=()=> 'Synthetic focus result',localizeKnownSystemMessage=s=>s;
const api=(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
const setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout=noop;
const document={title:'Elren',hidden:false};let focusCalls=0;
const window={dispatchEvent:noop,focus:()=>focusCalls++};const CustomEvent=function(){};
let taskId='a',taskViewGeneration=1,taskViewLoading=false,startRequestPending=false,uploadRequestPending=false;
let continuationTaskId=null,stopRequestPending=false,pollFailureCount=0,pollTimer=null,lastEventId=null;
let pendingHumanAction=null,surfacedHumanActionId=null,timelineUnseenProgressCount=0;
let currentTaskSnapshot=null;
const taskActionStates=new Map(),composerDrafts=new Map();
const renderArtifactInspector=noop,renderSubagentPanel=noop,updateModelBadge=noop,updateContextUsage=noop;
const updateTaskProgress=noop,updateTaskOverview=noop,restoreTimelineScrollState=noop;
const rebuildTaskTimeline=task=>{$('#timeline').innerHTML='Restored '+task.title;};
const clearTimelineNewProgress=noop,updateTimelineNewProgress=noop,renderTerminalState=noop;
const captureTimelineScrollState=()=>({follow:true}),countTimelineNewProgress=()=>0,isFinalAssistantEvent=()=>false;
const renderEvent=e=>renderedEvents.push(e),loadTaskHistory=noop;
const promptPlaceholder=()=>taskId?'Follow up on this running task':'New task';
const snapshot=(takenOver=false)=>({id:'a',title:'Synthetic title A',status:'waiting_user',events:[{id:'e1'},{id:'e2'}],
  pending_human_actions:[{id:'synthetic-human',task_id:'a',summary:'Synthetic manual action',instructions:'No action is performed.',taken_over:takenOver}]});
const approvalSnapshot=(tool='synthetic_write_tool')=>({id:'a',title:'Synthetic approval',status:'waiting_approval',events:[{id:'e1'},{id:'e2'}],
  pending_approvals:[{id:'synthetic-approval',tool,risk:'high',summary:'Synthetic confirmation',arguments:{action:'write'}}]});
const tick=async()=>{for(let i=0;i<5;i++)await Promise.resolve();};
let composerDraftScope='a',pendingAttachments=[],latestStatus={primary_key_configured:true};
let navigationIntent=0,terminalStatusRendered=null,timelineAutoFollow=true,dismissedArtifactInspectorTaskId=null;
const reasoningPreferenceValue=()=>'high',refreshModelPreferenceUI=noop,syncReasoningAvailability=noop;
const openWorkspacePanel=noop,activateSettingsSection=noop,hideArtifactInspector=noop,hideSubagentPanel=noop;
const taskSourceIcon=()=>'',escapeHtml=s=>String(s),sentAttachmentMarkup=()=>'',resizePromptInput=noop,renderAttachments=noop;
"""
CORE = (
    "taskActionKey", "updateTaskActionState", "reconcileTaskActionState",
    "syncTaskApproval", "resolveTaskApproval", "syncTaskHumanAction",
    "isCurrentTaskRequest", "isCurrentHumanActionRequest", "syncComposerAction",
    "taskComposerPlaceholder", "taskDisplayTitle", "updateConversationTitle",
    "poll", "mergeTaskEventUpdate", "eventDelta",
)


def run_js(body, *names):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend behavior checks")
    # Project behavior has its own executable suite; existing lifecycle fixtures
    # use the legacy application workspace and only stub this new UI dependency.
    script = PRELUDE + "\nconst syncProjectComposer=noop;let newTaskProjectPath='';\n" + "\n".join(function(name) for name in dict.fromkeys(("syncTaskViewUrl", *CORE, *names)))
    script += "\n(async()=>{\n" + body + "\nconsole.log(JSON.stringify({ok:true}));\n})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True,
                            encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


@pytest.mark.parametrize("tool", ["synthetic_write_tool", "synthetic_external_tool", "synthetic_unknown_tool"])
@pytest.mark.parametrize("approved", [True, False])
def test_mandatory_approval_never_auto_posts_and_explicit_choice_is_one_shot(tool, approved):
    run_js("const tool=" + json.dumps(tool) + ";const approved=" + json.dumps(approved) + ";" + r"""
      currentTaskSnapshot=approvalSnapshot(tool);
      const refresh=poll();requests[0].resolve(approvalSnapshot(tool));await refresh;
      assert.equal(pollFailureCount,0);assert.equal(requests.length,1);
      assert.equal($('#taskApproval').classList.contains('hidden'),false);
      assert.match($('#taskApprovalArguments').textContent,/write/);
      const click=approved?$('#confirmTaskApproval').onclick:$('#rejectTaskApproval').onclick;
      const posting=click();await click();
      assert.equal(requests.length,2);assert.equal(requests[1].url,'/api/approvals/synthetic-approval');
      assert.deepEqual(JSON.parse(requests[1].options.body),{approved});
      const whilePending=poll();requests[2].resolve(approvalSnapshot(tool));await whilePending;
      assert.equal($('#confirmTaskApproval').disabled,true);assert.equal($('#rejectTaskApproval').disabled,true);
      requests[1].resolve({ok:true});await posting;
      assert.equal($('#taskApproval').classList.contains('hidden'),true);
      const staleServer=poll();requests[3].resolve(approvalSnapshot(tool));await staleServer;
      assert.equal($('#taskApproval').classList.contains('hidden'),true);
      await click();assert.equal(requests.length,4);
    """)


def test_approval_failure_reenables_and_navigation_drops_old_ui_response():
    run_js(r"""
      currentTaskSnapshot=approvalSnapshot();syncTaskHumanAction(currentTaskSnapshot);
      const failed=$('#confirmTaskApproval').onclick();requests[0].reject(new Error('Synthetic offline'));await failed;
      assert.equal($('#confirmTaskApproval').disabled,false);assert.equal(currentTaskSnapshot.pending_approvals.length,1);
      const retry=$('#rejectTaskApproval').onclick();
      taskId='b';taskViewGeneration++;currentTaskSnapshot={...approvalSnapshot(),id:'b',title:'B',pending_approvals:[{id:'approval-b'}]};
      syncTaskHumanAction(currentTaskSnapshot);updateConversationTitle(currentTaskSnapshot);const count=notices.length;
      requests[1].resolve({ok:true});await retry;
      assert.equal($('#conversationTitle').textContent,'B');assert.equal($('#taskApproval').classList.contains('hidden'),false);
      assert.equal($('#confirmTaskApproval').disabled,false);assert.equal(notices.length,count);
      taskId='a';taskViewGeneration++;currentTaskSnapshot=reconcileTaskActionState(approvalSnapshot());syncTaskHumanAction(currentTaskSnapshot);
      assert.equal($('#taskApproval').classList.contains('hidden'),true);
    """)


def test_successful_approval_does_not_authorize_the_next_pending_operation():
    run_js(r"""
      currentTaskSnapshot=approvalSnapshot();currentTaskSnapshot.pending_approvals.push({id:'next-approval',tool:'synthetic_external_tool',arguments:{action:'write'}});
      syncTaskHumanAction(currentTaskSnapshot);const first=$('#confirmTaskApproval').onclick();requests[0].resolve({ok:true});await first;
      assert.equal(requests.length,1);assert.equal(currentTaskSnapshot.pending_approvals[0].id,'next-approval');
      assert.equal($('#taskApproval').classList.contains('hidden'),false);assert.equal($('#confirmTaskApproval').disabled,false);
      const next=$('#rejectTaskApproval').onclick();assert.equal(requests[1].url,'/api/approvals/next-approval');
      assert.deepEqual(JSON.parse(requests[1].options.body),{approved:false});requests[1].resolve({ok:true});await next;
    """)


def test_takeover_ack_survives_old_and_fresh_but_stale_server_get():
    run_js(r"""
      currentTaskSnapshot=snapshot(false);syncTaskHumanAction(currentTaskSnapshot);
      const oldGet=poll();const takeover=takeOverHumanAction();await takeOverHumanAction();
      assert.equal(requests.length,2);
      requests[1].resolve({request:snapshot(true).pending_human_actions[0],focus:{}});await takeover;
      requests[0].resolve(snapshot(false));await oldGet;
      assert.equal(pendingHumanAction.taken_over,true);assert.equal($('#completeHumanAction').disabled,false);
      const fresh=poll();requests[2].resolve(snapshot(false));await fresh;
      assert.equal(pollFailureCount,0);assert.equal(pendingHumanAction.taken_over,true);
      assert.equal($('#takeOverHumanAction').disabled,true);assert.equal($('#completeHumanAction').disabled,false);
    """, "takeOverHumanAction")


@pytest.mark.parametrize("completed", [True, False])
def test_manual_pending_poll_failure_retry_and_ack_do_not_resurrect_request(completed):
    run_js("const completed=" + json.dumps(completed) + ";" + r"""
      currentTaskSnapshot=snapshot(true);syncTaskHumanAction(currentTaskSnapshot);
      const first=resolveHumanAction(completed,'Synthetic issue');
      const pendingGet=poll();requests[1].resolve(snapshot(true));await pendingGet;
      assert.equal($('#completeHumanAction').disabled,true);assert.equal($('#cancelHumanAction').disabled,true);
      await resolveHumanAction(true);assert.equal(requests.length,2);
      requests[0].reject(new Error('Synthetic failure'));await first;
      assert.equal($('#completeHumanAction').disabled,false);assert.equal($('#cancelHumanAction').disabled,false);
      const oldGet=poll();const retry=resolveHumanAction(completed,'Synthetic issue');
      assert.equal(JSON.parse(requests[3].options.body).completed,completed);
      requests[3].resolve({ok:true});await retry;requests[2].resolve(snapshot(true));await oldGet;
      assert.equal(pendingHumanAction,null);assert.equal($('#humanAction').classList.contains('hidden'),true);
      const stale=poll();requests[4].resolve(snapshot(true));await stale;
      assert.equal(pollFailureCount,0);assert.equal(pendingHumanAction,null);
      assert.equal($('#humanAction').classList.contains('hidden'),true);
      assert.notEqual($('#prompt').placeholder,'Complete the current manual action first');
    """, "resolveHumanAction")


@pytest.mark.parametrize("method", ["takeOverHumanAction()", "resolveHumanAction(true)"])
@pytest.mark.parametrize("failure", [False, True])
def test_manual_response_after_navigation_never_changes_other_task(method, failure):
    run_js("const takeover=" + json.dumps(method.startswith("take")) + ";" + r"""
      currentTaskSnapshot=snapshot(!takeover);syncTaskHumanAction(currentTaskSnapshot);
    """ + f"const request={method};" + r"""
      taskId='b';taskViewGeneration++;currentTaskSnapshot={id:'b',title:'B',status:'waiting_user',pending_human_actions:[{id:'human-b',taken_over:false}]};
      syncTaskHumanAction(currentTaskSnapshot);updateConversationTitle(currentTaskSnapshot);const count=notices.length;
    """ + ("requests[0].reject(new Error('Synthetic failure'));" if failure else "requests[0].resolve({ok:true,request:snapshot(true).pending_human_actions[0],focus:{}});") + r"""
      await request;assert.equal(pendingHumanAction.id,'human-b');assert.equal($('#conversationTitle').textContent,'B');
      assert.equal($('#completeHumanAction').disabled,true);assert.equal($('#takeOverHumanAction').disabled,false);
      assert.equal(notices.length,count);assert.equal(timers.length,0);
    """, "takeOverHumanAction", "resolveHumanAction")


def test_ack_during_full_snapshot_fallback_is_not_rolled_back():
    run_js(r"""
      currentTaskSnapshot=snapshot(true);syncTaskHumanAction(currentTaskSnapshot);
      const refresh=poll();requests[0].resolve({...snapshot(true),events:[],event_delta:true});await tick();
      assert.equal(requests.length,2,'Expected missing-cursor fallback GET');
      const completion=resolveHumanAction(true);requests[2].resolve({ok:true});await completion;
      requests[1].resolve(snapshot(true));await refresh;
      assert.equal(pendingHumanAction,null);assert.equal($('#humanAction').classList.contains('hidden'),true);
    """, "resolveHumanAction")


def test_takeover_failure_can_retry_without_changing_another_manual_request():
    run_js(r"""
      currentTaskSnapshot=snapshot(false);syncTaskHumanAction(currentTaskSnapshot);
      const failure=takeOverHumanAction();requests[0].reject(new Error('Synthetic offline'));await failure;
      assert.equal(pendingHumanAction.taken_over,false);assert.equal($('#takeOverHumanAction').disabled,false);
      const retry=takeOverHumanAction();
      currentTaskSnapshot={...snapshot(false),pending_human_actions:[{id:'next-human',taken_over:false}]};syncTaskHumanAction(currentTaskSnapshot);
      const count=notices.length;requests[1].resolve({request:snapshot(true).pending_human_actions[0],focus:{}});await retry;
      assert.equal(pendingHumanAction.id,'next-human');assert.equal(pendingHumanAction.taken_over,false);
      assert.equal($('#completeHumanAction').disabled,true);assert.equal(notices.length,count);
    """, "takeOverHumanAction")


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
def test_terminal_snapshots_hide_both_approval_and_human_residuals(status):
    run_js("const status=" + json.dumps(status) + ";" + r"""
      currentTaskSnapshot={...approvalSnapshot(),pending_human_actions:snapshot(true).pending_human_actions};syncTaskHumanAction(currentTaskSnapshot);
      currentTaskSnapshot={...currentTaskSnapshot,status};syncTaskHumanAction(currentTaskSnapshot);
      assert.equal($('#taskApproval').classList.contains('hidden'),true);assert.equal($('#humanAction').classList.contains('hidden'),true);
      assert.equal(pendingHumanAction,null);await resolveTaskApproval('a','synthetic-approval',true);assert.equal(requests.length,0);
      syncTaskHumanAction(null);assert.equal($('#taskApproval').classList.contains('hidden'),true);
    """)


def test_stop_pending_blocks_followup_approval_and_human_actions_until_failure():
    run_js(r"""
      currentTaskSnapshot={...approvalSnapshot(),pending_human_actions:snapshot(true).pending_human_actions};syncTaskHumanAction(currentTaskSnapshot);
      $('#prompt').value='Unsent draft';const stop=requestStop();
      assert.equal($('#send').disabled,true);assert.equal($('#confirmTaskApproval').disabled,true);assert.equal($('#completeHumanAction').disabled,true);
      await resolveTaskApproval('a','synthetic-approval',true);await resolveHumanAction(true);await sendRunningMessage('Unsent draft');await start();
      assert.equal(requests.length,1,'No second operation while cancellation is in flight');
      requests[0].resolve({ok:false});await stop;
      assert.equal($('#send').disabled,false);assert.equal($('#stopWaitingTask').disabled,false);
      assert.equal($('#confirmTaskApproval').disabled,false);assert.equal($('#completeHumanAction').disabled,false);
    """, "requestStop", "resolveHumanAction", "sendRunningMessage", "start")


@pytest.mark.parametrize("failure", [True, False])
def test_stop_response_after_navigation_does_not_lock_new_task(failure):
    run_js(r"""
      currentTaskSnapshot=snapshot(true);syncTaskHumanAction(currentTaskSnapshot);const stop=requestStop();
      taskId='b';taskViewGeneration++;stopRequestPending=false;currentTaskSnapshot={id:'b',status:'running'};
      $('#prompt').value='B draft';syncTaskHumanAction(currentTaskSnapshot);syncComposerAction();const count=notices.length;
    """ + ("requests[0].reject(new Error('Synthetic failure'));" if failure else "requests[0].resolve({ok:true});") + r"""
      await stop;assert.equal(stopRequestPending,false);assert.equal($('#prompt').value,'B draft');assert.equal($('#send').disabled,false);
      assert.equal(notices.length,count);assert.equal(timers.length,0);
    """, "requestStop")


@pytest.mark.parametrize("status", ["waiting_user", "waiting_approval"])
def test_waiting_task_can_stop_without_losing_draft_and_retry_after_failure(status):
    run_js("const status=" + json.dumps(status) + ";" + r"""
      currentTaskSnapshot={id:'a',status};syncComposerAction();
      assert.equal($('#send').dataset.action,'stop');assert.equal($('#send').disabled,false);
      $('#prompt').value='Unsent draft';pendingAttachments=[{path:'synthetic.pdf'}];syncComposerAction();
      assert.equal($('#stopWaitingTask').hidden,false);assert.equal($('#stopWaitingTask').disabled,false);
      const stop=requestStop();await requestStop();assert.equal(requests.length,1);
      assert.equal(requests[0].url,'/api/tasks/a/cancel');assert.equal(requests[0].options.method,'POST');
      assert.equal($('#stopWaitingTask').disabled,true);
      requests[0].reject(new Error('Synthetic offline'));await stop;
      assert.equal($('#stopWaitingTask').disabled,false);assert.equal($('#prompt').value,'Unsent draft');
      assert.deepEqual(pendingAttachments,[{path:'synthetic.pdf'}]);
      const retry=requestStop();requests[1].resolve({ok:true});await retry;
      const refresh=poll();requests[2].resolve({id:'a',status:'cancelled',events:[]});await refresh;syncComposerAction();
      assert.equal(taskId,null);assert.equal(continuationTaskId,'a');assert.equal($('#stopWaitingTask').hidden,true);
      assert.equal($('#prompt').value,'Unsent draft');assert.deepEqual(pendingAttachments,[{path:'synthetic.pdf'}]);
    """, "requestStop")


CONTINUE = ("continueTaskImmediately", "start", "originalContinuationModel", "clearSubmittedDraft", "clearSubmittedComposer")
CONTINUE_SETUP = r"""
taskId=null;continuationTaskId='a';currentTaskSnapshot={id:'a',title:'Original task',status:'failed',events:[],active_model:'original-model'};
$('#conversationTitle').textContent='Original task';$('#modelPreference').options=[{value:'original-model'},{value:'other-model'}];
"""


def test_incremental_poll_preserves_archived_turns():
    run_js(r"""
      const archive=[{prompt:'First question',result:'First answer',status:'completed',events:[]}];
      const previous={id:'a',events:[{id:'old'},{id:'cursor'}],conversation_turns:archive};
      const update={id:'a',event_delta:true,events:[{id:'cursor'},{id:'new'}]};
      const merged=mergeTaskEventUpdate(previous,update);
      assert.equal(merged.conversation_turns,archive);
      assert.deepEqual(merged.events.map(e=>e.id),['old','cursor','new']);
      assert.equal(mergeTaskEventUpdate(previous,{...update,events:[{id:'new-run'}]}),null);
    """)


@pytest.mark.parametrize("status", ["queued", "running", "waiting_approval", "waiting_user", "completed", "failed", "cancelled"])
def test_all_states_send_only_to_selected_conversation(status):
    run_js("const status=" + json.dumps(status) + ";" + r"""
      const terminal=['completed','failed','cancelled'].includes(status);
      taskId=terminal?null:'a';continuationTaskId=terminal?'a':null;
      currentTaskSnapshot={id:'a',title:'Original task',status,events:[]};
      $('#conversationTitle').textContent='Original task';$('#prompt').value='Follow up';
      syncComposerAction();
      assert.equal($('#send').disabled,status==='waiting_user');
      const posting=start();await tick();
      if(status==='waiting_user'){
        assert.equal(await posting,false);assert.equal(requests.length,0);
        assert.equal($('#prompt').value,'Follow up');
      } else {
      assert.equal(requests.length,1);
      assert.equal(requests[0].url,terminal?'/api/tasks/a/continue':'/api/tasks/a/messages');
      assert.equal($('#conversationTitle').textContent,'Original task');
      assert.equal(await start(),false);assert.equal(requests.length,1);
      requests[0].resolve({id:'a',title:'Original task',status:'running',events:[]});
      assert.equal(await posting,true);assert.equal(taskId,'a');
      assert.equal($('#conversationTitle').textContent,'Original task');
      }
    """, "start", "sendRunningMessage", "clearSubmittedDraft", "clearSubmittedComposer")


@pytest.mark.parametrize("model", ["", "other-model"])
def test_continue_failure_keeps_user_instruction_attachments_and_original_title(model):
    run_js(CONTINUE_SETUP + "const selected=" + json.dumps(model) + ";" + r"""
      $('#prompt').value='My correction';pendingAttachments=[{path:'synthetic.pdf'}];
      const request=continueTaskImmediately(currentTaskSnapshot,selected);
      assert.equal($('#prompt').value,'My correction');const body=JSON.parse(requests[0].options.body);
      assert.equal(body.prompt,'My correction');assert.deepEqual(body.attachments,['synthetic.pdf']);
      assert.equal(body.model_preference,selected||'original-model');assert.equal(requests[0].url,'/api/tasks/a/continue');
      requests[0].reject(new Error('Synthetic offline'));assert.equal(await request,false);
      assert.equal($('#prompt').value,'My correction');assert.deepEqual(pendingAttachments,[{path:'synthetic.pdf'}]);
      assert.equal($('#conversationTitle').textContent,'Original task');assert.equal(currentTaskSnapshot.status,'failed');
      assert.equal($('#timeline').innerHTML,'Restored Original task');
    """, *CONTINUE)


@pytest.mark.parametrize("new_typing", [False, True])
def test_continue_success_clears_only_submitted_draft(new_typing):
    run_js(CONTINUE_SETUP + r"""
      $('#prompt').value='My correction';pendingAttachments=[{path:'old.pdf'}];
      const request=continueTaskImmediately(currentTaskSnapshot);
    """ + ("$('#prompt').value='New unsent text';pendingAttachments.push({path:'new.pdf'});" if new_typing else "") + r"""
      requests[0].resolve({id:'a',title:'Original task',status:'running',events:[]});assert.equal(await request,true);
    """ + ("assert.equal($('#prompt').value,'New unsent text');assert.deepEqual(pendingAttachments,[{path:'new.pdf'}]);" if new_typing else "assert.equal($('#prompt').value,'');assert.deepEqual(pendingAttachments,[]);") + r"""
      assert.equal(currentTaskSnapshot.status,'running');assert.equal(startRequestPending,false);
    """, *CONTINUE)


def test_empty_continue_is_request_fallback_and_missing_configuration_does_not_edit_draft():
    run_js(CONTINUE_SETUP + r"""
      const request=continueTaskImmediately(currentTaskSnapshot);
      assert.equal($('#prompt').value,'');assert.match(JSON.parse(requests[0].options.body).prompt,/Continue the previous task/);
      $('#prompt').value='Typed during request';requests[0].reject(new Error('Synthetic offline'));await request;
      assert.equal($('#prompt').value,'Typed during request');assert.equal($('#conversationTitle').textContent,'Original task');
      latestStatus={primary_key_configured:false};assert.equal(await continueTaskImmediately(currentTaskSnapshot),false);
      assert.equal(requests.length,1);assert.equal($('#prompt').value,'Typed during request');
      assert.equal($('#conversationTitle').textContent,'Original task');
    """, *CONTINUE)


@pytest.mark.parametrize("failure", [True, False])
def test_continue_after_navigation_does_not_restore_old_title_or_touch_new_draft(failure):
    run_js(CONTINUE_SETUP + r"""
      $('#prompt').value='A correction';const request=continueTaskImmediately(currentTaskSnapshot);
      taskViewGeneration++;taskId='b';currentTaskSnapshot={id:'b',title:'B',status:'running'};
      composerDrafts.set('a',{text:'A correction',attachments:[]});composerDraftScope='b';
      $('#conversationTitle').textContent='B';$('#prompt').value='B draft';
    """ + ("requests[0].reject(new Error('Synthetic offline'));" if failure else "requests[0].resolve({id:'a',title:'Original task',status:'running',events:[]});") + r"""
      await request;
      assert.equal($('#conversationTitle').textContent,'B');assert.equal($('#prompt').value,'B draft');assert.equal(renderedEvents.length,0);
    """, *CONTINUE)
