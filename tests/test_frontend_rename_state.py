"""Deferred rename responses must remain attached to their dialog and task."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = (Path(__file__).resolve().parents[1] / "deepdesk/static/app.js").read_text("utf-8")


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
    value:'', disabled:false, open:false, attributes:{},
    setAttribute(key,value){this.attributes[key]=value;},
    close(){this.open=false;}, showModal(){this.open=true;}, focus(){}, select(){},
  });
  return elements.get(selector);
};
const uiText = (zh,en) => en;
const toasts = [], titles = [], requests = [];
const showWorkspaceToast = (...args) => toasts.push(args);
const updateConversationTitle = task => titles.push(task.title);
const loadTaskHistory = async() => {};
const api = (url,options) => new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
let renameTaskId = null, renameTaskDialogGeneration = 0, contextTaskId = null;
const pendingTaskRenames = new Set();
let currentTaskSnapshot = {id:'a',title:'Original A',status:'running',events:[]};
const rows = ['a','b'].map(id=>({dataset:{taskId:id},querySelector(){return {textContent:'Original '+id};}}));
const document = {querySelectorAll(){return rows;}};
const closeTaskContextMenu = () => {contextTaskId=null;};
const event = {preventDefault(){}};
function openRename(id,title) {
  contextTaskId=id;
  renameTaskFromContextMenu();
  if(title) $('#renameTaskInput').value=title;
}
"""


def run_js(body, *extra_functions, prelude=""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend behavior checks")
    names = (
        "syncRenameTaskDialogState", "closeRenameTaskDialog",
        "renameTaskFromContextMenu", "submitTaskRename", *extra_functions,
    )
    script = PRELUDE + prelude + "\n" + "\n".join(function(name) for name in names)
    script += "\n(async()=>{\n" + body + "\nconsole.log(JSON.stringify({ok:true}));\n})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False,
                            encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}


def test_double_submit_issues_one_patch_and_preserves_server_normalized_title():
    run_js("""
      openRename('a',' - New   A - ');
      const before = currentTaskSnapshot;
      const first = submitTaskRename(event);
      await submitTaskRename(event);
      assert.equal(requests.length,1);
      assert.equal($('#saveRenameTask').disabled,true);
      assert.equal($('#renameTaskForm').attributes['aria-busy'],'true');
      assert.deepEqual(JSON.parse(requests[0].options.body),{title:'- New   A -'});
      requests[0].resolve({title:'New A'});
      await first;
      assert.notEqual(currentTaskSnapshot,before);
      assert.equal(before.title,'Original A');
      assert.equal(currentTaskSnapshot.title,'New A');
      assert.equal(currentTaskSnapshot.events,before.events);
      assert.equal($('#renameTaskDialog').open,false);
      assert.equal($('#saveRenameTask').disabled,false);
      assert.equal(pendingTaskRenames.size,0);
    """)


def test_old_success_cannot_close_or_enable_another_tasks_pending_dialog():
    run_js("""
      openRename('a','New A');
      const first = submitTaskRename(event);
      closeRenameTaskDialog();
      openRename('b','New B');
      currentTaskSnapshot = {id:'b',title:'Original B'};
      const second = submitTaskRename(event);
      requests[0].resolve({title:'New A'});
      await first;
      assert.equal(renameTaskId,'b');
      assert.equal($('#renameTaskInput').value,'New B');
      assert.equal($('#renameTaskDialog').open,true);
      assert.equal($('#saveRenameTask').disabled,true);
      assert.equal(currentTaskSnapshot.title,'Original B');
      assert.equal(toasts.length,0);
      requests[1].resolve({title:'New B'});
      await second;
      assert.equal(currentTaskSnapshot.title,'New B');
      assert.equal($('#renameTaskDialog').open,false);
      assert.equal($('#saveRenameTask').disabled,false);
    """)


def test_closing_and_reopening_same_task_does_not_reuse_old_dialog_identity():
    run_js("""
      openRename('a','New A');
      const first = submitTaskRename(event);
      closeRenameTaskDialog();
      openRename('a','Newer draft A');
      await submitTaskRename(event);
      assert.equal(requests.length,1);
      requests[0].resolve({title:'New A'});
      await first;
      assert.equal(renameTaskId,'a');
      assert.equal($('#renameTaskInput').value,'Newer draft A');
      assert.equal($('#renameTaskDialog').open,true);
      assert.equal($('#saveRenameTask').disabled,false);
      assert.equal(toasts.length,0);
    """)


def test_typing_a_newer_name_while_saving_preserves_the_unsaved_input():
    run_js("""
      openRename('a','Submitted A');
      const first = submitTaskRename(event);
      $('#renameTaskInput').value = 'Unsaved newer A';
      requests[0].resolve({title:'Submitted A'});
      await first;
      assert.equal(currentTaskSnapshot.title,'Submitted A');
      assert.equal($('#renameTaskInput').value,'Unsaved newer A');
      assert.equal($('#renameTaskDialog').open,true);
      assert.equal($('#saveRenameTask').disabled,false);
      assert.match(toasts[0][0],/newer changes are still unsaved/);
    """)


def test_old_failure_does_not_report_error_in_a_new_dialog():
    run_js("""
      openRename('a','New A');
      const first = submitTaskRename(event);
      closeRenameTaskDialog();
      openRename('b','New B');
      requests[0].reject(Error('Old request failed'));
      await first;
      assert.equal(renameTaskId,'b');
      assert.equal($('#renameTaskDialog').open,true);
      assert.equal($('#renameTaskInput').value,'New B');
      assert.equal(toasts.length,0);
      assert.equal(pendingTaskRenames.size,0);
    """)


def test_failure_keeps_input_and_enables_retry_in_the_original_dialog():
    run_js("""
      openRename('a','New A');
      const first = submitTaskRename(event);
      requests[0].reject(Error('Offline'));
      await first;
      assert.equal(currentTaskSnapshot.title,'Original A');
      assert.equal($('#renameTaskInput').value,'New A');
      assert.equal($('#renameTaskDialog').open,true);
      assert.equal($('#saveRenameTask').disabled,false);
      assert.match(toasts[0][0],/Rename failed: Offline/);
      const retry = submitTaskRename(event);
      assert.equal(requests.length,2);
      requests[1].resolve({title:'New A'});
      await retry;
      assert.equal(currentTaskSnapshot.title,'New A');
    """)


def test_in_flight_poll_cannot_roll_back_a_successful_rename():
    run_js("""
      const refresh = poll();
      openRename('a','Renamed A');
      const rename = submitTaskRename(event);
      requests[1].resolve({title:'Renamed A'});
      await rename;
      requests[0].resolve({id:'a',title:'Original A',status:'running',events:[]});
      await refresh;
      assert.equal(currentTaskSnapshot.title,'Renamed A');
      assert.deepEqual(titles,['Renamed A']);
      assert.equal(timers.length,1);
    """, "poll", "isCurrentTaskRequest", prelude="""
      let taskId='a', taskViewGeneration=1, taskViewLoading=false, pollTimer=null;
      const timers=[];
      const setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length;};
    """)
