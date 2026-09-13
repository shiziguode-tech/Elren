"""Check chat navigation as observable UI behavior with delayed responses."""

import pytest
from test_frontend_async_workflows import run_js

NAVIGATION_PRELUDE = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const placeholderWrites = [];
const $ = selector => {
  if (!elements.has(selector)) {
    const classes = new Set();
    const attributes = new Map();
    let placeholder = '';
    elements.set(selector, {
      value:'', textContent:'', title:'', disabled:false, dataset:{},
      classList:{
        add(...names){names.forEach(name => classes.add(name));},
        remove(...names){names.forEach(name => classes.delete(name));},
        toggle(name, force){
          const enabled = force === undefined ? !classes.has(name) : force;
          if (enabled) classes.add(name); else classes.delete(name);
        },
        contains(name){return classes.has(name);},
      },
      setAttribute(name,value){attributes.set(name,String(value));},
      getAttribute(name){return attributes.get(name);},
      removeAttribute(name){attributes.delete(name);},
      querySelector(){return null;},
      querySelectorAll(){return [];},
      focus(){},
      get placeholder(){return placeholder;},
      set placeholder(value){
        placeholder = value;
        if (selector === '#prompt') placeholderWrites.push(value);
      },
    });
  }
  return elements.get(selector);
};
const uiText = (zh,en) => en;
const isEnglish = () => true;
const promptPlaceholder = () => taskId
  ? 'You can send follow-ups while this task runs; the Agent will receive them after the current step…'
  : 'Describe what you want to get done…';
const document = {title:'Elren'};
const window = {dispatchEvent(){},focus(){}};
const CustomEvent = class {constructor(type, options){this.type=type;this.detail=options.detail;}};
let taskId = null, continuationTaskId = null, currentTaskSnapshot = null;
const taskActionStates = new Map();
let taskViewGeneration = 0, navigationIntent = 0, pollTimer = null;
let lastEventId = null, pendingHumanAction = null, surfacedHumanActionId = null;
let terminalStatusRendered = null, stopRequestPending = false;
let pollFailureCount = 0, timelineAutoFollow = true, dismissedArtifactInspectorTaskId = null;
let startRequestPending = false, taskViewLoading = false;
const rememberNewTaskComposerPreferences = () => {};
const hideArtifactInspector = () => {};
const hideSubagentPanel = () => {};
const clearTimelineNewProgress = () => {};
const closeNavigation = () => {};
const setPrimaryNavigation = () => {};
const refreshModelPreferenceUI = () => {};
const syncReasoningAvailability = () => {};
const renderArtifactInspector = () => {};
const renderSubagentPanel = () => {};
const updateModelBadge = () => {};
const updateContextUsage = () => {};
const rebuildTaskTimeline = () => {};
const restoreTimelineScrollState = () => {};
const updateTaskProgress = () => {};
const updateTaskOverview = () => {};
const syncComposerAction = () => {};
const switchComposerDraft = () => {};
const syncSettingsSelectWidget = () => {};
const showToast = () => {};
const reset = () => {throw Error('Navigation unexpectedly reset the selected chat');};
let blankWelcomeCalls = 0;
const setBlankTaskWelcome = () => {blankWelcomeCalls++;$('#conversationTitle').textContent='New task';};
let historyCalls = 0;
let loadTaskHistory = () => {historyCalls++;return new Promise(()=>{});};
const pollCalls = [];
const poll = (...args) => {pollCalls.push(args);};
const apiCalls = [];
let api = async url => {apiCalls.push(url);throw Error('Unexpected API request');};
const settleMicrotasks = async () => {for(let i=0;i<8;i++) await Promise.resolve();};
const doesNotWaitForHistory = async request => {
  const settled = await Promise.race([
    request.then(()=>true),
    new Promise(resolve=>setTimeout(()=>resolve(false),100)),
  ]);
  assert.equal(settled,true,'Opening a chat must not wait for its sidebar refresh');
};
"""


OPEN_TASK_FUNCTIONS = (
    "openTask", "isCurrentTaskRequest", "taskComposerPlaceholder",
    "updateConversationTitle", "taskDisplayTitle", "isBlankNewTaskComposer",
    "syncTaskHumanAction", "taskActionKey", "reconcileTaskActionState", "syncTaskApproval",
)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_chat_is_ready_after_first_response_without_waiting_for_history(status):
    run_js(r"""
      const status = STATUS;
      api = async url => {
        apiCalls.push(url);
        return {id:'saved',title:'Saved score conversion',status,events:[]};
      };
      const request = openTask('saved');
      await settleMicrotasks();
      assert.equal(currentTaskSnapshot.id,'saved');
      assert.equal(taskId,null);
      assert.equal(continuationTaskId,'saved');
      assert.equal($('#conversationTitle').textContent,'Saved score conversion');
      assert.match($('#prompt').placeholder,/^Continue this task:/);
      assert(!placeholderWrites.some(value=>/manual action/i.test(value)),
        'A terminal chat must never briefly display a manual-takeover instruction');
      assert.equal(historyCalls,1);
      assert.deepEqual(apiCalls,['/api/tasks/saved']);
      assert.deepEqual(pollCalls,[],'A terminal snapshot does not need another task GET');
      await doesNotWaitForHistory(request);
    """.replace("STATUS", repr(status)), *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_chats_navigation_keeps_the_selected_terminal_chat_and_title(status):
    run_js(r"""
      taskId = null;
      continuationTaskId = 'saved';
      currentTaskSnapshot = {id:'saved',title:'Saved score conversion',status:STATUS};
      $('#conversationTitle').textContent = currentTaskSnapshot.title;
      loadTaskHistory = async () => {historyCalls++;};
      for(let i=0;i<5;i++) await showHistoryNavigation('','navChats');
      assert.equal(blankWelcomeCalls,0);
      assert.equal($('#conversationTitle').textContent,'Saved score conversion');
      assert.equal(continuationTaskId,'saved');
      assert.equal(currentTaskSnapshot.id,'saved');
    """.replace("STATUS", repr(status)), "showHistoryNavigation", "isBlankNewTaskComposer",
        prelude=NAVIGATION_PRELUDE)


def test_real_waiting_user_chat_keeps_manual_action_hint_without_waiting_for_history():
    run_js(r"""
      api = async url => {
        apiCalls.push(url);
        return {id:'manual',title:'Needs verification',status:'waiting_user',events:[],
          pending_human_actions:[{id:'verification',summary:'Verify this step',
            instructions:'Review the browser page',taken_over:false}]};
      };
      const request = openTask('manual');
      await settleMicrotasks();
      assert.equal(taskId,'manual');
      assert.equal(continuationTaskId,null);
      assert.match($('#prompt').placeholder,/manual action/i);
      assert.equal(pendingHumanAction.id,'verification');
      assert.equal($('#humanActionSummary').textContent,'Verify this step');
      assert(!$('#humanAction').classList.contains('hidden'));
      assert.equal($('#takeOverHumanAction').disabled,false);
      assert.equal($('#completeHumanAction').disabled,true);
      assert.deepEqual(pollCalls,[['manual',taskViewGeneration]],
        'The active task poll must start even while history refresh is pending');
      await doesNotWaitForHistory(request);
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


def test_switching_chats_immediately_hides_the_previous_manual_takeover_panel():
    run_js(r"""
      taskId = 'manual';
      currentTaskSnapshot = {id:'manual',status:'waiting_user'};
      pendingHumanAction = {id:'old-action'};
      surfacedHumanActionId = 'old-action';
      $('#humanAction').classList.remove('hidden');
      let resolveTask;
      api = () => new Promise(resolve=>{resolveTask=resolve;});
      const request = openTask('saved');
      assert($('#humanAction').classList.contains('hidden'),
        'The previous task takeover controls must disappear before the new request returns');
      assert.equal(pendingHumanAction,null);
      assert.equal(surfacedHumanActionId,null);
      resolveTask({id:'saved',title:'Saved score conversion',status:'completed',events:[]});
      await doesNotWaitForHistory(request);
      assert($('#humanAction').classList.contains('hidden'));
      assert.match($('#prompt').placeholder,/^Continue this task:/);
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


def test_out_of_order_chat_responses_cannot_restore_an_old_manual_action_hint_or_title():
    run_js(r"""
      const pending = new Map();
      api = url => new Promise(resolve=>{pending.set(url,resolve);});
      const oldRequest = openTask('old-manual');
      const newestRequest = openTask('saved');
      pending.get('/api/tasks/saved')({id:'saved',title:'Newest selection',status:'completed',events:[]});
      await doesNotWaitForHistory(newestRequest);
      pending.get('/api/tasks/old-manual')({id:'old-manual',title:'Old selection',status:'waiting_user',events:[]});
      await oldRequest;
      assert.equal(currentTaskSnapshot.id,'saved');
      assert.equal(continuationTaskId,'saved');
      assert.equal($('#conversationTitle').textContent,'Newest selection');
      assert.match($('#prompt').placeholder,/^Continue this task:/);
      assert(!placeholderWrites.some(value=>/manual action/i.test(value)));
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


def test_reselecting_loaded_terminal_chat_does_not_refetch_or_reset_composer():
    run_js(r"""
      continuationTaskId = 'saved';
      currentTaskSnapshot = {id:'saved',title:'Saved score conversion',status:'completed',events:[]};
      $('#conversationTitle').textContent = currentTaskSnapshot.title;
      $('#prompt').placeholder = 'Continue this task: enter the next instruction…';
      $('#prompt').value = 'My next instruction';
      placeholderWrites.length = 0;
      for(let i=0;i<5;i++) await openTask('saved');
      assert.deepEqual(apiCalls,[]);
      assert.deepEqual(pollCalls,[]);
      assert.deepEqual(placeholderWrites,[]);
      assert.equal($('#prompt').value,'My next instruction');
      assert.equal($('#conversationTitle').textContent,'Saved score conversion');
      assert.equal(continuationTaskId,'saved');
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


def test_terminal_snapshot_cannot_surface_residual_manual_action():
    run_js(r"""
      pendingHumanAction = {id:'old-action'};
      surfacedHumanActionId = 'old-action';
      $('#humanAction').classList.remove('hidden');
      document.title = 'Action required · Elren';
      syncTaskHumanAction({id:'saved',status:'completed',pending_human_actions:[{
        id:'stale-action',summary:'No longer pending',taken_over:false,
      }]});
      assert.equal(pendingHumanAction,null);
      assert.equal(surfacedHumanActionId,null);
      assert($('#humanAction').classList.contains('hidden'));
      assert.equal(document.title,'Elren');
    """, "syncTaskHumanAction", "taskActionKey", "reconcileTaskActionState", "syncTaskApproval", prelude=NAVIGATION_PRELUDE)


def test_reselecting_the_same_loading_chat_uses_one_in_flight_request():
    run_js(r"""
      let resolveTask;
      api = url => {
        apiCalls.push(url);
        return new Promise(resolve=>{resolveTask=resolve;});
      };
      const request = openTask('saved');
      const generation = taskViewGeneration;
      assert.equal(taskViewLoading,true);
      for(let i=0;i<5;i++) await openTask('saved');
      assert.deepEqual(apiCalls,['/api/tasks/saved']);
      assert.equal(taskViewGeneration,generation);
      assert.equal(taskViewLoading,true);
      resolveTask({id:'saved',title:'Saved selection',status:'completed',events:[]});
      await doesNotWaitForHistory(request);
      assert.equal(taskViewLoading,false);
      assert.equal(continuationTaskId,'saved');
      assert.equal($('#conversationTitle').textContent,'Saved selection');
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


def test_obsolete_response_does_not_release_the_new_chat_loading_guard():
    run_js(r"""
      const pending = new Map();
      api = url => new Promise(resolve=>{pending.set(url,resolve);});
      const oldRequest = openTask('old');
      const newestRequest = openTask('new');
      pending.get('/api/tasks/old')({id:'old',status:'completed',events:[]});
      await oldRequest;
      assert.equal(taskId,'new');
      assert.equal(taskViewLoading,true,
        'An old request finally block must not enable actions on a still-loading chat');
      pending.get('/api/tasks/new')({id:'new',title:'Newest selection',status:'completed',events:[]});
      await doesNotWaitForHistory(newestRequest);
      assert.equal(taskViewLoading,false);
      assert.equal(continuationTaskId,'new');
      assert.equal($('#conversationTitle').textContent,'Newest selection');
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


@pytest.mark.parametrize("action", [
    "await start()", "await sendRunningMessage('Do not submit yet')",
    "await requestStop()", "await continueTaskImmediately(currentTaskSnapshot)",
])
def test_composer_actions_wait_until_new_chat_snapshot_is_loaded(action):
    run_js(r"""
      taskId = 'new-selection';
      currentTaskSnapshot = {id:'old-selection',status:'running'};
      taskViewLoading = true;
      $('#prompt').value = 'Do not submit yet';
      ACTION;
      assert.deepEqual(apiCalls,[]);
      assert.deepEqual(pollCalls,[]);
      assert.equal($('#prompt').value,'Do not submit yet');
      assert.equal(taskId,'new-selection');
      assert.equal(currentTaskSnapshot.id,'old-selection');
    """.replace("ACTION", action), "start", "sendRunningMessage", "requestStop",
        "continueTaskImmediately", prelude=NAVIGATION_PRELUDE + "\nlet uploadRequestPending = false;")
