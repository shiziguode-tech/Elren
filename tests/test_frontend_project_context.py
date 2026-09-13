"""Execute production project-composer JavaScript with isolated deferred I/O."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_frontend_action_lifecycle import PRELUDE, function
from test_frontend_language_transfer import run_js as run_language_js


def test_project_picker_removed_without_breaking_composer_initialization():
    index = (Path(__file__).resolve().parents[1] / "deepdesk/static/index.html").read_text("utf-8")
    assert 'id="projectPath"' not in index
    assert 'id="projectPathLabel"' not in index
    assert 'id="projectPathHelp"' not in index
    assert 'class="composer-project"' not in index
    source = function.__globals__["SOURCE"]
    listener = re.search(r'^\$\("#projectPath"\)\?\.addEventListener\([\s\S]*?^\}\);', source, re.MULTILINE)
    assert listener
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    script = "const $=()=>null;\n" + function("syncProjectComposer") + "\nsyncProjectComposer();\n" + listener.group()
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def run_js(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    extra = r"""
let newTaskProjectPath='E:/Developer project';
const viewMutations=[];
$('#projectPathLabel').lastChild={textContent:''};
const updateConversationTitle=()=>viewMutations.push('title'),syncTaskViewUrl=noop,poll=noop,clearSubmittedDraft=noop;
const taskComposerPlaceholder=()=>'';
let followups=[];
const sendRunningMessage=async text=>{followups.push(text);return true;};
taskId=null;composerDraftScope=null;
$('#prompt').value='Implement feature';
"""
    source = function.__globals__["SOURCE"]
    listener = re.search(
        r'^\$\("#projectPath"\)\?\.addEventListener\("input", \(event\) => \{([\s\S]*?)^\}\);',
        source, re.MULTILINE,
    )
    assert listener
    prelude = PRELUDE.replace("updateTaskProgress=noop,updateTaskOverview=noop", "updateTaskProgress=()=>viewMutations.push('progress'),updateTaskOverview=()=>viewMutations.push('overview')")
    script = prelude + extra + "\n" + "\n".join(function(name) for name in (
        "syncProjectComposer", "syncComposerAction", "start",
    )) + "\nconst projectInput=event=>{" + listener.group(1) + "};\n"
    script += "(async()=>{" + body + "\nconsole.log('ok')})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True,
                            encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_new_task_project_is_editable_and_survives_history_round_trip():
    run_js(r"""
syncProjectComposer();assert.equal($('#projectPath').readOnly,false);
assert.equal($('#projectPath').value,'E:/Developer project');
$('#projectPath').value='E:/Next repo';projectInput({currentTarget:$('#projectPath')});
taskId='history';currentTaskSnapshot={id:'history',status:'running',project_path:'E:/History'};
syncProjectComposer();assert.equal($('#projectPath').value,'E:/History');assert.equal($('#projectPath').readOnly,true);
taskId=null;currentTaskSnapshot=null;syncProjectComposer();
assert.equal($('#projectPath').value,'E:/Next repo');assert.equal($('#projectPath').readOnly,false);
""")


@pytest.mark.parametrize("status", ["queued", "running", "waiting_user", "waiting_approval", "completed", "failed", "cancelled"])
def test_all_historical_states_keep_bound_project_readonly(status):
    run_js("const status=" + json.dumps(status) + r""";
currentTaskSnapshot={id:'history',status,project_path:'E:/Bound'};
taskId=['completed','failed','cancelled'].includes(status)?null:'history';
continuationTaskId=taskId?null:'history';syncProjectComposer();
assert.equal($('#projectPath').value,'E:/Bound');assert.equal($('#projectPath').readOnly,true);
projectInput({currentTarget:{readOnly:true,value:'E:/Wrong'}});
assert.equal(newTaskProjectPath,'E:/Developer project');
""")


def test_start_locks_project_immediately_and_posts_validated_captured_path():
    run_js(r"""
syncProjectComposer();const posting=start();
assert.equal(startRequestPending,true);assert.equal($('#projectPath').readOnly,true);
assert.equal(requests[0].url,'/api/projects/validate');
assert.equal(JSON.parse(requests[0].options.body).project_path,'E:/Developer project');
projectInput({currentTarget:{readOnly:$('#projectPath').readOnly,value:'E:/Wrong'}});
assert.equal(newTaskProjectPath,'E:/Developer project');
requests[0].resolve({project_path:'E:/Developer project'});await tick();
assert.equal(requests[1].url,'/api/tasks');
assert.equal(JSON.parse(requests[1].options.body).project_path,'E:/Developer project');
requests[1].resolve({id:'created',status:'queued',project_path:'E:/Developer project'});
assert.equal(await posting,true);assert.equal($('#projectPath').readOnly,true);
""")


def test_invalid_project_validation_preserves_draft_and_does_not_create_task():
    run_js(r"""
$('#timeline').innerHTML='Original timeline';$('#welcome').classList.remove('hidden');
$('#timeline').classList.add('hidden');lastEventId='preserved-event';terminalStatusRendered='preserved-terminal';
const posting=start();assert.deepEqual(viewMutations,[]);assert.equal($('#timeline').innerHTML,'Original timeline');
requests[0].reject(Error('Project not found'));
assert.equal(await posting,false);assert.equal(requests.length,1);
assert.equal($('#prompt').value,'Implement feature');
assert.equal(newTaskProjectPath,'E:/Developer project');assert.equal($('#projectPath').readOnly,false);
assert.deepEqual(renderedEvents,[]);assert.deepEqual(viewMutations,[]);
assert.equal($('#timeline').innerHTML,'Original timeline');assert.equal($('#welcome').classList.contains('hidden'),false);
assert.equal($('#timeline').classList.contains('hidden'),true);assert.equal(lastEventId,'preserved-event');
assert.equal(terminalStatusRendered,'preserved-terminal');assert.equal(startRequestPending,false);assert.equal($('#send').disabled,false);
assert.equal(notices.length,1);assert.equal(notices[0][0],'Project folder unavailable: Project not found');
""")


def test_navigating_during_project_preflight_does_not_mutate_new_view():
    run_js(r"""
const posting=start();taskViewGeneration++;$('#timeline').innerHTML='Navigated elsewhere';
requests[0].resolve({project_path:'E:/Developer project'});assert.equal(await posting,false);
assert.equal(requests.length,1);assert.deepEqual(viewMutations,[]);assert.equal(notices.length,0);
assert.equal($('#timeline').innerHTML,'Navigated elsewhere');assert.equal(startRequestPending,false);
""")


@pytest.mark.parametrize("message,translated", [
    ("Select a project folder, not a drive root or home directory", "请选择具体项目文件夹，不能使用磁盘根目录或用户主目录"),
    ("Project directory does not exist or cannot be accessed", "项目目录不存在或无法访问"),
    ("Project paths cannot contain symbolic links or junctions", "项目路径不能包含符号链接或目录联接"),
])
def test_project_preflight_errors_are_localized_without_losing_reason(message, translated):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    script = "const assert=require('node:assert/strict');let english=false;const isEnglish=()=>english;\n" + function("localizeKnownSystemMessage")
    script += "\nconst original=" + json.dumps(message) + ";assert.equal(localizeKnownSystemMessage(original)," + json.dumps(translated) + ");english=true;assert.equal(localizeKnownSystemMessage(original),original);"
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr


def test_terminal_continuation_never_resubmits_new_project():
    run_js(r"""
continuationTaskId='old';currentTaskSnapshot={id:'old',status:'completed',project_path:'E:/Original'};
const posting=start();assert.equal(requests.length,1);
assert.equal(requests[0].url,'/api/tasks/old/continue');
assert.equal(Object.hasOwn(JSON.parse(requests[0].options.body),'project_path'),false);
requests[0].resolve({id:'old',status:'queued',project_path:'E:/Original'});
assert.equal(await posting,true);assert.equal($('#projectPath').value,'E:/Original');
""")


def test_running_followup_does_not_validate_or_rebind_project():
    run_js(r"""
taskId='running';currentTaskSnapshot={id:'running',status:'running',project_path:'E:/Original'};
assert.equal(await start(),true);assert.equal(requests.length,0);
assert.deepEqual(followups,['Implement feature']);
""")


def test_loading_project_cannot_be_edited():
    run_js("taskViewLoading=true;syncProjectComposer();assert.equal($('#projectPath').readOnly,true);")


def test_language_reload_discards_hidden_legacy_project_without_url_leak():
    result = run_language_js(r"""
newTaskProjectPath='E:/中文 project & code';const next=transfer();freshPage();
console.log(JSON.stringify({ok:restoreLanguageComposerTransfer(),path:newTaskProjectPath,
 leaked:next.toString().includes('project'),remaining:storage.size}));
""")
    assert result == {"ok": True, "path": "", "leaked": False, "remaining": 0}


@pytest.mark.parametrize("value", ["{}", "[]", "42", "null", "'a'.repeat(4097)", "'bad\\0path'"])
def test_language_reload_rejects_malformed_project_atomically(value):
    result = run_language_js("""
transfer();const key=[...storage.keys()][0],s=JSON.parse(storage.get(key));
s.newTaskProject=""" + value + """;storage.set(key,JSON.stringify(s));freshPage();
console.log(JSON.stringify({ok:restoreLanguageComposerTransfer(),path:newTaskProjectPath,
 count:composerDrafts.size,text:$('#prompt').value,remaining:storage.size}));
""")
    assert result == {"ok": False, "path": "", "count": 0, "text": "", "remaining": 0}


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("legacy_failure", [False, True])
def test_verification_failure_is_critical_localized_and_escaped(language, legacy_failure):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    script = "const assert=require('node:assert/strict');const language=" + json.dumps(language) + r""";
const uiText=(zh,en)=>language==='en'?en:zh;
const appSymbol=()=>'',localizedToolName=()=>'',modelDisplayName=()=>'',localizedEventTypeLabel=()=>'';
let html='';const $=()=>({insertAdjacentHTML:(_where,value)=>{html+=value;}});
""" + "\n".join(function(name) for name in (
        "escapeHtml", "isFailedToolResult", "eventPresentation", "verificationWarningMarkup", "renderEvent",
    ))
    script += "const legacy=" + json.dumps(legacy_failure) + r""";
const failure={command:'npm test <img src=x onerror="alert(1)">'},
 data=legacy?{failure}:{failures:[failure,{tool:'shell & checks'}]};
renderEvent({type:'verification_unresolved',data});
assert.match(html,/critical-event/);assert(!html.includes('optional-event'));
assert(!html.includes('<img'));assert(html.includes('&lt;img'));assert(html.includes('&quot;'));
assert.match(html,/<li><code>npm test/);
if(!legacy)assert(html.includes('shell &amp; checks'));
if(language==='en'){assert(html.includes('Unresolved verification failures'));assert(html.includes('changes are not verified'));assert(!html.includes('验收'));}
else {assert(html.includes('仍有验证未通过'));assert(html.includes('不能据此认定改动已验收'));assert(!html.includes('changes are not verified'));}
assert(!html.includes('artifact-quality-verified'));console.log('ok');
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True,
                            encoding="utf-8", timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
