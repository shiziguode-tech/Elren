"""Exercise user-visible races with deferred responses, without a live provider."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "deepdesk/static/app.js").read_text("utf-8")


def function(name):
    match = re.search(rf"^(?:async )?function {name}\(", SOURCE, re.MULTILINE)
    assert match
    opening = SOURCE.index(") {", match.start()) + 2
    depth = 0
    for index in range(opening, len(SOURCE)):
        depth += (SOURCE[index] == "{") - (SOURCE[index] == "}")
        if depth == 0:
            return SOURCE[match.start():index + 1]
    raise AssertionError(name)


PRELUDE = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const $ = selector => {
  if (!elements.has(selector)) elements.set(selector, {
    value:'', innerHTML:'old history', textContent:'', disabled:false, scrollTop:0,
    dataset:{}, classList:{add(){},remove(){},toggle(){},contains(){return false;}},
    setAttribute(){}, removeAttribute(){}, querySelectorAll(){return [];},
    insertAdjacentHTML(){ throw Error('Unexpected DOM append'); },
  });
  return elements.get(selector);
};
const uiText = (zh,en) => en;
const isEnglish = () => true;
const localizeKnownSystemMessage = text => text;
const escapeHtml = text => String(text);
const showToast = () => {};
const showWorkspaceToast = () => {};
const renderArtifactInspector = () => { throw Error('Stale task rendered'); };
const renderSubagentPanel = () => {};
const resizePromptInput = () => {};
const renderAttachments = () => {};
const syncComposerAction = () => {};
const syncTaskHumanAction = () => {};
let composerDraftScope = 'a';
const composerDrafts = new Map();
const clearSubmittedDraft = () => {};
let taskId = 'a', taskViewGeneration = 1, startRequestPending = false;
let taskViewLoading = false;
let uploadRequestPending = false, stopRequestPending = false;
let currentTaskSnapshot = {id:'a',status:'running'}, pendingAttachments = [];
let pollTimer = null;
const poll = () => { throw Error('Unexpected polling'); };
let resolveRequest;
let api = () => new Promise(resolve => { resolveRequest = resolve; });
let historyLoading = false, historyReloadPending = false, historyReloadWaiters = [];
let historyOffset = 50, historyHasMore = true, historyFirstPageSignature = '';
let historyLoadedFilter = JSON.stringify(['','']);
let knownHistoryTaskIds = new Set(['old']), navigationIntent = 0;
const HISTORY_PAGE_SIZE = 50;
"""


def run_js(body, *names, prelude=PRELUDE):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend behavior checks")
    dependencies = ("syncTaskViewUrl",) if any(name in names for name in ("openTask", "start")) else ()
    script = prelude + "\n" + "\n".join(function(name) for name in dict.fromkeys((*dependencies, *names)))
    script += "\n(async()=>{\n" + body + "\nconsole.log(JSON.stringify({ok:true}));\n})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False,
                            encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


@pytest.mark.parametrize("english,language", [(True, "en"), (False, "zh")])
def test_api_passes_current_ui_language_and_displays_structured_upload_error(english, language):
    run_js("""
      let message = '';
      try { await api('/api/uploads', {method:'POST', body:new ArrayBuffer(0)}); }
      catch (error) { message = error.message; }
      assert.equal(message, 'localized upload error');
    """, "api", prelude=f"""
      const assert = require('node:assert/strict');
      const isEnglish = () => {str(english).lower()};
      const fetch = async (url, options) => {{
        assert.equal(options.headers['Accept-Language'], '{language}');
        return {{ok:false, status:422, json:async()=>({{detail:{{
          code:'upload_empty_file', message:'localized upload error'
        }}}})}};
      }};
    """)


def test_follow_up_response_does_not_replace_a_different_chat_or_erase_its_draft():
    run_js("""
      $('#prompt').value = 'message for A';
      const request = sendRunningMessage('message for A');
      taskId = 'b'; taskViewGeneration++;
      currentTaskSnapshot = {id:'b',status:'running'};
      $('#prompt').value = 'unsent draft for B';
      resolveRequest({id:'a',status:'running'});
      await request;
      assert.equal(currentTaskSnapshot.id,'b');
      assert.equal($('#prompt').value,'unsent draft for B');
      assert.equal(startRequestPending,false);
    """, "sendRunningMessage", "isCurrentTaskRequest")


def test_successful_send_keeps_newer_typing_and_newer_attachments():
    run_js("""
      $('#prompt').value = 'next instruction';
      pendingAttachments = [{path:'sent.pdf'},{path:'new.pdf'}];
      clearSubmittedComposer('sent instruction',[{path:'sent.pdf'}]);
      assert.equal($('#prompt').value,'next instruction');
      assert.deepEqual(pendingAttachments,[{path:'new.pdf'}]);
      clearSubmittedComposer('next instruction',[{path:'new.pdf'}]);
      assert.equal($('#prompt').value,'');
      assert.deepEqual(pendingAttachments,[]);
    """, "clearSubmittedComposer")


def test_upload_finishing_after_navigation_does_not_attach_to_new_chat():
    run_js("""
      const request = uploadFiles([{name:'score.png',arrayBuffer:async()=>new ArrayBuffer(0)}]);
      await Promise.resolve();
      assert.equal(uploadRequestPending,true);
      taskViewGeneration++;
      composerDraftScope = 'b';
      resolveRequest({path:'score.png',name:'score.png'});
      await request;
      assert.deepEqual(pendingAttachments,[]);
      assert.equal(composerDrafts.get('a').attachments[0].path,'score.png');
      assert.equal(uploadRequestPending,false);
      assert.equal($('#uploadFile').disabled,false);
    """, "uploadFiles")


def test_send_waits_for_all_attachments_to_finish_uploading():
    run_js("""
      uploadRequestPending = true;
      let calls = 0;
      api = async()=>{calls++;};
      assert.equal(await sendRunningMessage('use this score'),false);
      assert.equal(calls,0);
    """, "sendRunningMessage")


@pytest.mark.parametrize("status", ["waiting_user", "completed", "failed", "cancelled"])
def test_direct_send_cannot_bypass_task_status(status):
    run_js("currentTaskSnapshot.status = " + json.dumps(status) + ";" + """
      let calls = 0;
      api = async()=>{calls++;};
      assert.equal(await sendRunningMessage('follow-up'),false);
      assert.equal(calls,0);
    """, "sendRunningMessage")


def test_continue_during_upload_cannot_replace_the_users_draft():
    run_js("""
      uploadRequestPending = true;
      $('#prompt').value = 'my draft';
      await continueTaskImmediately({id:'a',status:'failed'});
      assert.equal($('#prompt').value,'my draft');
      assert.equal(taskId,'a');
    """, "continueTaskImmediately")


def test_stale_cancel_response_cannot_poll_or_change_new_chat():
    run_js("""
      const request = requestStop();
      taskId = 'b'; taskViewGeneration++; stopRequestPending = false;
      resolveRequest({ok:false,message:'already stopped'});
      await request;
      assert.equal(pollTimer,null);
      assert.equal(stopRequestPending,false);
    """, "requestStop", "isCurrentTaskRequest")


def test_background_connection_failure_keeps_readable_history():
    run_js("""
      api = async()=>{throw Error('offline');};
      await loadTaskHistory({background:true});
      assert.equal($('#taskHistory').innerHTML,'old history');
      assert.equal(historyOffset,50);
      assert.equal(historyLoading,false);
    """, "loadTaskHistory")


def test_append_response_for_old_filter_is_discarded_and_refresh_waits():
    run_js("""
      const append = loadTaskHistory({append:true});
      $('#historySearch').value = 'new filter';
      let refreshed = false;
      const refresh = loadTaskHistory().then(()=>{refreshed=true;});
      await Promise.resolve();
      assert.equal(refreshed,false);
      resolveRequest({tasks:[{id:'wrong-filter'}]});
      api = async()=>({tasks:[],total:0,has_more:false});
      await append;
      await refresh;
      assert.equal(refreshed,true);
      assert.equal(historyOffset,0);
      assert(!$('#taskHistory').innerHTML.includes('wrong-filter'));
    """, "loadTaskHistory", "historyResultSignature")


def test_background_update_keeps_loaded_older_pages_while_reading_them():
    run_js("""
      historyOffset = 100;
      $('#taskHistory').scrollTop = 2300;
      api = async()=>({tasks:[{id:'new',status:'running'}],total:101});
      await loadTaskHistory({background:true});
      assert.equal(historyOffset,100);
      assert.equal($('#taskHistory').scrollTop,2300);
      assert.equal($('#taskHistory').innerHTML,'old history');
    """, "loadTaskHistory", "historyResultSignature")


def test_old_running_navigation_cannot_reset_the_users_new_selection():
    run_js("""
      const request = showHistoryNavigation('running','navRunning');
      navigationIntent++;
      resolveRequest();
      await request;
      assert.equal(currentTaskSnapshot.id,'a');
    """, "showHistoryNavigation", prelude=PRELUDE + """
      const syncSettingsSelectWidget = () => {};
      const setPrimaryNavigation = () => {};
      const closeNavigation = () => {};
      const loadTaskHistory = () => api();
      const reset = () => {throw Error('Stale navigation reset the task');};
    """)


def test_schedule_creation_cannot_be_double_submitted_and_ignores_hidden_end():
    run_js("""
      $('#scheduleKind').value = 'at';
      $('#scheduleStartAt').value = '2027-01-01T09:00';
      $('#scheduleEndAt').value = '2020-01-01T09:00';
      let calls = [];
      api = (url, options) => {calls.push(JSON.parse(options.body));return new Promise(r=>{resolveRequest=r;});};
      const event = {preventDefault(){}};
      const first = createSchedule(event);
      await createSchedule(event);
      assert.equal(calls.length,1);
      assert.equal(calls[0].end_at,null);
      resolveRequest({ok:true}); await first;
      assert.equal(scheduleCreatePending,false);
      assert.equal($("#scheduleForm button[type='submit']").disabled,false);
    """, "createSchedule", "scheduleFormSignature", "parseScheduleDateTime", prelude=PRELUDE + """
      let scheduleCreatePending = false;
      const resetScheduleForm = () => {};
      const loadSchedules = async() => {};
    """)
