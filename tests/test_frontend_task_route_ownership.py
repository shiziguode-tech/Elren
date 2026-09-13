"""Navigation commits its URL only after the current task request succeeds."""

import json

import pytest
from test_frontend_action_lifecycle import CONTINUE, CONTINUE_SETUP
from test_frontend_action_lifecycle import run_js as run_action
from test_frontend_async_workflows import run_js
from test_frontend_chat_navigation import NAVIGATION_PRELUDE, OPEN_TASK_FUNCTIONS

URL_SETUP = r"""
window.location={href:'http://127.0.0.1:8923/?lang=en&task=A&ui_release=qa#details'};
const urlWrites=[];
window.history={replaceState:(_a,_b,url)=>{urlWrites.push(url);window.location.href=url;}};
"""


@pytest.mark.parametrize("status", ["running", "completed", "failed"])
def test_open_commits_task_route_and_preserves_unrelated_query_and_hash(status):
    run_js(URL_SETUP + "const status=" + json.dumps(status) + ";" + r"""
      let resolve;api=()=>new Promise(r=>resolve=r);const p=openTask('B');
      assert.equal(new URL(window.location.href).searchParams.get('task'),'A');
      resolve({id:'B',title:'B',status,events:[]});await p;
      const route=new URL(window.location.href);assert.equal(route.searchParams.get('task'),'B');
      assert.equal(route.searchParams.get('lang'),'en');assert.equal(route.searchParams.get('ui_release'),'qa');
      assert.equal(route.hash,'#details');assert.equal(currentTaskSnapshot.id,'B');assert.equal(urlWrites.length,1);
      await openTask('B');assert.equal(urlWrites.length,1,'Reselection should not churn the address');
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


@pytest.mark.parametrize("old_failure", [False, True])
def test_old_task_reply_cannot_change_newly_committed_route(old_failure):
    run_js(URL_SETUP + "const oldFailure=" + json.dumps(old_failure) + ";" + r"""
      const pending=[];api=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
      const a=openTask('old');const b=openTask('B');
      pending[1].resolve({id:'B',title:'B',status:'completed',events:[]});await b;
      const route=window.location.href;
      if(oldFailure)pending[0].reject(Error('old network failure'));
      else pending[0].resolve({id:'old',title:'old',status:'running',events:[]});
      await a;assert.equal(window.location.href,route);assert.equal(urlWrites.length,1);
      assert.equal(currentTaskSnapshot.id,'B');
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


def test_reselecting_a_loaded_task_repairs_stale_route_without_requesting_again():
    run_js(URL_SETUP + r"""
      currentTaskSnapshot={id:'B',title:'B',status:'completed'};continuationTaskId='B';
      await openTask('B');assert.equal(new URL(window.location.href).searchParams.get('task'),'B');
      assert.equal(apiCalls.length,0);assert.equal(urlWrites.length,1);
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


def test_task_open_still_succeeds_when_history_api_is_unavailable():
    run_js(URL_SETUP + r"""
      window.history.replaceState=()=>{throw Error('Synthetic history unavailable');};
      api=async()=>({id:'B',title:'B',status:'completed',events:[]});
      await openTask('B');assert.equal(currentTaskSnapshot.id,'B');assert.equal(continuationTaskId,'B');
    """, *OPEN_TASK_FUNCTIONS, prelude=NAVIGATION_PRELUDE)


def test_route_helper_encodes_task_ids_and_clear_removes_only_task():
    run_js(URL_SETUP + r"""
      syncTaskViewUrl('合成 &/# id');assert.equal(new URL(window.location.href).searchParams.get('task'),'合成 &/# id');
      syncTaskViewUrl(null);const route=new URL(window.location.href);
      assert.equal(route.searchParams.has('task'),false);assert.equal(route.searchParams.get('lang'),'en');
      assert.equal(route.searchParams.get('ui_release'),'qa');assert.equal(route.hash,'#details');
    """, "syncTaskViewUrl", prelude=NAVIGATION_PRELUDE)


def test_failed_current_navigation_resets_view_and_route_without_claiming_failed_task_opened():
    prelude = NAVIGATION_PRELUDE.replace(
        "const reset = () => {throw Error('Navigation unexpectedly reset the selected chat');};", ""
    ) + "\nconst restoreNewTaskComposerPreferences=()=>{},resizePromptInput=()=>{};\n"
    run_js(URL_SETUP + r"""
      api=async()=>{throw Error('Synthetic missing task');};
      await openTask('missing');const route=new URL(window.location.href);
      assert.equal(route.searchParams.has('task'),false);assert.equal(currentTaskSnapshot,null);
      assert.equal(taskId,null);assert.equal(continuationTaskId,null);
      assert.equal(route.searchParams.get('lang'),'en');assert.equal(route.hash,'#details');
    """, *OPEN_TASK_FUNCTIONS, "reset", "clearSystemComposerDraft", prelude=prelude)


@pytest.mark.parametrize("late_navigation", [False, True])
def test_task_creation_or_continuation_does_not_leave_previous_route(late_navigation):
    run_action(CONTINUE_SETUP + URL_SETUP + "const late=" + json.dumps(late_navigation) + ";" + r"""
      $('#prompt').value='Synthetic instruction';const p=continueTaskImmediately(currentTaskSnapshot);
      if(late){
        taskViewGeneration++;taskId='B';currentTaskSnapshot={id:'B',title:'B',status:'running'};
        composerDrafts.set('a',{text:'Synthetic instruction',attachments:[]});composerDraftScope='B';
        syncTaskViewUrl('B');
      }
      requests[0].resolve({id:'a',title:'Original task',status:'running',events:[]});await p;
      assert.equal(new URL(window.location.href).searchParams.get('task'),late?'B':'a');
      assert.equal(urlWrites.length,1);
    """, *CONTINUE)


def test_successful_new_task_creation_commits_its_new_identifier():
    run_action(URL_SETUP + r"""
      taskId=null;continuationTaskId=null;currentTaskSnapshot=null;composerDraftScope=null;
      $('#prompt').value='Synthetic new task';const p=start();
      assert.equal(requests[0].url,'/api/tasks');assert.equal(urlWrites.length,0);
      requests[0].resolve({id:'created',title:'Synthetic new task',status:'running',events:[]});
      assert.equal(await p,true);assert.equal(new URL(window.location.href).searchParams.get('task'),'created');
      assert.equal(currentTaskSnapshot.id,'created');assert.equal(urlWrites.length,1);
    """, *CONTINUE)
