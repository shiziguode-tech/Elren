"""Real settings load/edit/save/close/reopen chains over synthetic API state."""

import json
import re
import shutil
import subprocess

import pytest
from test_frontend_omission_repairs import (
    COMMON,
    INDEX,
    SETTINGS,
    SETTINGS_REPAIR_HELPERS,
    SETTINGS_SETUP,
    function,
)


def run_js(body, *, result_expression=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    common = COMMON.replace('const api=(url,options)=>', 'let api=(url,options)=>')
    setup = SETTINGS_SETUP.replace(
        "let requestSettingsSnapshot=()=>{settingsSnapshotPromise=Promise.resolve({...savedSettings});return settingsSnapshotPromise;};", ""
    ).replace("renderModelProviderRows=noop", "renderModelProviderRows=entries=>{providers=entries.map(e=>({id:e.id,provider:e.provider,model:e.model,api_key:e.api_key||''}));}")
    setup = setup.replace("renderDiscussionTeamRows=noop", "renderDiscussionTeamRows=entries=>{team=structuredClone(entries);}")
    setup = setup.replace("collectDiscussionTeamRows=()=>[],collectModelProviderRows=()=>[]", "collectDiscussionTeamRows=()=>structuredClone(team),collectModelProviderRows=()=>structuredClone(providers)")
    setup = setup.replace("const askForConfirmation=async()=>confirmDiscard", "const askForConfirmation=async(message)=>{confirmations.push(message);return confirmDiscard;}")
    setup = setup.replace("populateSettingVoiceNames=noop", "populateSettingVoiceNames=name=>{$('#settingVoiceName').value=name;}")
    setup = setup.replace("readDiscussionTeamDraft=()=>null,discussionTeamDraftDiffers=()=>false", "readDiscussionTeamDraft=()=>recoverableTeam,discussionTeamDraftDiffers=(draft,saved)=>!!draft&&JSON.stringify(draft)!==JSON.stringify(saved)")
    controls = re.findall(r'<(?:input|select|textarea)\b[^>]*\bid="(setting[^"]+)"', INDEX)
    checkboxes = re.findall(r'<input\b[^>]*\bid="(setting[^"]+)"[^>]*type="checkbox"', INDEX)
    names = (*SETTINGS, *SETTINGS_REPAIR_HELPERS, "requestSettingsSnapshot", "openWorkspacePanel", "markSettingsFormDirty")
    program = common + "\n" + json.dumps(controls) + ".forEach(id=>$('#'+id));\n"
    program += json.dumps(checkboxes) + ".forEach(id=>$('#'+id).type='checkbox');\n"
    program += "let providers=[],team=[],recoverableTeam=null;const confirmations=[];\n" + setup + r"""
const SETTINGS_SNAPSHOT_REUSE_MS=15000;
let navigationIntent=0;
const closeNavigation=noop,hideSubagentPanel=noop,enhanceWorkspaceSelects=noop,applyWorkspaceEnglish=noop;
const scheduleActiveSettingsSectionAlignment=noop,loadArtifacts=noop,loadSchedules=noop;
HTMLInputElement.prototype.showModal=function(){this.open=true;};
HTMLInputElement.prototype.listeners=null;
HTMLInputElement.prototype.addEventListener=function(type,fn){this.listeners ||= new Map();this.listeners.set(type,fn);};
document.querySelectorAll=selector=>selector==='.workspace-panel'?[$('#panel-settings'),$('#panel-artifacts')]:[];
$('#panel-settings').classList.add('active');
const settle=async()=>{for(let i=0;i<16;i++)await Promise.resolve();};
const teamEntries=name=>[
 {id:'leader',name,role:'leader',model:'auto',reasoning_effort:'default',assignment:'',system_prompt:''},
 {id:'member',name:'Reviewer',role:'member',model:'auto',reasoning_effort:'default',assignment:'',system_prompt:''},
];
Object.assign(savedSettings,{model:'synthetic-model',reasoning_effort:'high',voice_name:'voice-a',voice_rate:1,
 voice_language:'auto',voice_auto_speak:true,
 voice_hands_free:true,voice_auto_continue:true,cross_conversation_context:true,custom_system_prompt_suffix:'',
 max_output_tokens:null,model_providers:[]});
let backend=structuredClone(savedSettings),getDeferred=false;
const reads=[],posts=[];
api=(url,options)=>{
  assert.equal(url,'/api/settings');
  if(!options){
    const snapshot=structuredClone(backend);
    if(getDeferred)return new Promise((resolve,reject)=>reads.push({snapshot,resolve,reject}));
    reads.push({snapshot});return Promise.resolve(snapshot);
  }
  assert.equal(options.method,'PATCH');const patch=JSON.parse(options.body);
  return new Promise((resolve,reject)=>posts.push({patch,resolve,reject,
    commit(extra={}){backend={...backend,...patch};resolve({...structuredClone(backend),...extra});}}));
};
async function loaded(){settingsFormDirty=false;await loadSettings();assert.equal(settingsFormHydrating,false);assert(settingsPreferenceBaseline);}
function edit(selector,value){const c=$(selector);if(c.type==='checkbox')c.checked=value;else c.value=String(value);markSettingsFormDirty();}
"""
    program += "\n" + "\n".join(function(n) for n in dict.fromkeys(names))
    output = "console.log(JSON.stringify(" + result_expression + "));" if result_expression else "console.log('passed');"
    program += "\n(async()=>{\n" + body + "\n" + output + "})().catch(e=>{console.error(e);process.exit(1)});"
    result = subprocess.run([node, "-"], input=program, capture_output=True, text=True,
                            encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    if result_expression:
        return json.loads(result.stdout)
    assert result.stdout.strip() == "passed"


@pytest.mark.parametrize("field,selector,value", [
    ("request_timeout", "#settingTimeout", 300),
    ("voice_auto_speak", "#settingVoiceAutoSpeak", False),
    ("voice_hands_free", "#settingVoiceHandsFree", False),
    ("voice_auto_continue", "#settingVoiceAutoContinue", False),
    ("voice_rate", "#settingVoiceRate", 1.5),
    ("voice_name", "#settingVoiceName", "voice-b"),
    ("voice_language", "#settingVoiceLanguage", "en"),
    ("cross_conversation_context", "#settingCrossConversationContext", False),
    ("custom_system_prompt_suffix", "#settingCustomSystemPromptSuffix", "Synthetic instruction"),
    ("model", "#settingModel", "other-model"),
    ("reasoning_effort", "#settingReasoningEffort", "max"),
])
def test_only_locally_changed_scalar_is_patched(field, selector, value):
    run_js("const field=" + json.dumps(field) + ";const selector=" + json.dumps(selector)
           + ";const value=" + json.dumps(value) + ";" + r"""
      await loaded();edit(selector,value);const p=saveSettings({preventDefault(){}});
      assert.deepEqual(posts[0].patch,{[field]:value});posts[0].commit();await p;
      assert.equal(backend[field],value);assert.equal(settingsFormDirty,false);
    """)


def test_unrelated_save_retains_other_entry_changes_and_rehydrates_them():
    run_js(r"""
      await loaded();backend.request_timeout=300;backend.custom_system_prompt_suffix='Other entry';
      backend.discussion_team=teamEntries('External');
      backend.model_providers=[{id:'external-provider',provider:'openai',model:'synthetic',api_key:''}];
      edit('#settingVoiceAutoSpeak',false);const p=saveSettings({preventDefault(){}});
      assert.deepEqual(posts[0].patch,{voice_auto_speak:false});posts[0].commit();await p;
      assert.equal(backend.request_timeout,300);assert.equal($('#settingTimeout').value,300);
      assert.equal($('#settingCustomSystemPromptSuffix').value,'Other entry');
      assert.equal(team[0].name,'External');assert.equal(providers[0].id,'external-provider');
      assert.equal(settingsFormDirty,false);assert.equal(settingsPreferenceBaseline.request_timeout,300);
    """)


def test_empty_save_does_not_rewrite_unmodified_preferences():
    run_js(r"""
      await loaded();backend.request_timeout=300;const p=saveSettings({preventDefault(){}});
      assert.deepEqual(posts[0].patch,{});posts[0].commit();await p;
      assert.equal($('#settingTimeout').value,300);assert.equal(settingsFormDirty,false);
    """)


def test_recovered_team_draft_remains_clean_but_explicit_save_applies_it():
    run_js(r"""
      recoverableTeam=teamEntries('Recovered');await loaded();
      assert.equal(settingsFormDirty,false);assert.equal(recoveredDiscussionTeamDraftPending,true);
      assert.deepEqual(settingsPreferenceBaseline.discussion_team,[]);
      assert.equal(team[0].name,'Recovered');
      const p=saveSettings({preventDefault(){}});
      assert.deepEqual(posts[0].patch,{discussion_team:recoverableTeam});posts[0].commit();await p;
      assert.equal(settingsFormDirty,false);assert.equal(recoveredDiscussionTeamDraftPending,false);
      assert.equal(settingsPreferenceBaseline.discussion_team[0].name,'Recovered');
    """)


def test_discard_clears_dynamic_provider_key_inputs_alongside_fixed_keys():
    run_js(r"""
      await loaded();const key=$('#syntheticDynamicProviderKey');key.value='SYNTHETIC-DRAFT';
      const query=document.querySelectorAll;
      document.querySelectorAll=selector=>selector==='.model-provider-key'?[key]:query(selector);
      edit('#settingGithubToken','SYNTHETIC-FIXED');
      assert.equal(await requestCloseWorkspaceDialog(),true);
      assert.equal(key.value,'');assert.equal($('#settingGithubToken').value,'');
      assert.equal(posts.length,0);
    """)


def test_auto_output_null_and_changed_lists_are_deliberate_patches():
    run_js(r"""
      await loaded();edit('#settingAutoOutputTokens',false);edit('#settingMaxOutputTokens',2048);
      let p=saveSettings({preventDefault(){}});assert.deepEqual(posts[0].patch,{max_output_tokens:2048});posts[0].commit();await p;
      edit('#settingAutoOutputTokens',true);team=teamEntries('New');markSettingsFormDirty();
      p=saveSettings({preventDefault(){}});assert.deepEqual(posts[1].patch,{max_output_tokens:null,discussion_team:team});posts[1].commit();await p;
      assert.equal(settingsFormDirty,false);assert.equal(settingsPreferenceBaseline.max_output_tokens,null);
    """)


@pytest.mark.parametrize("newer_edit", [False, True])
def test_close_reopen_pending_save_merges_ack_and_preserves_new_visit_edits(newer_edit):
    run_js("const newer=" + json.dumps(newer_edit) + ";" + r"""
      await loaded();edit('#settingTimeout',300);const p=saveSettings({preventDefault(){}});
      assert.equal(await requestCloseWorkspaceDialog(),true);assert(confirmations.at(-1).includes('will not cancel'));
      openWorkspacePanel('settings');await settle();assert.equal($('#settingTimeout').value,120);
      if(newer){edit('#settingTimeout',350);edit('#settingGithubToken','SYNTHETIC-NEW');}
      posts[0].commit();await p;
      assert.equal($('#settingTimeout').value,newer?'350':300);assert.equal(settingsFormDirty,newer);
      assert.equal($('#settingGithubToken').value,newer?'SYNTHETIC-NEW':'');
      assert.equal(settingsPreferenceBaseline.request_timeout,300);
      edit('#settingVoiceAutoSpeak',false);const next=saveSettings({preventDefault(){}});
      assert.deepEqual(posts[1].patch,newer?{request_timeout:350,voice_auto_speak:false,github_token:'SYNTHETIC-NEW'}:{voice_auto_speak:false});
      posts[1].commit();await next;assert.equal(backend.request_timeout,newer?350:300);
    """)


def test_save_ack_followed_by_late_precommit_reopen_get_cannot_restore_old_clean_form():
    run_js(r"""
      await loaded();edit('#settingTimeout',300);const p=saveSettings({preventDefault(){}});
      await requestCloseWorkspaceDialog();getDeferred=true;openWorkspacePanel('settings');await settle();
      assert.equal(reads.length,2);posts[0].commit();await settle();assert.equal(reads.length,3);
      reads[2].resolve(reads[2].snapshot);await p;
      reads[1].resolve(reads[1].snapshot);await settle();
      assert.equal($('#settingTimeout').value,300);assert.equal(settingsFormDirty,false);
      assert.equal(settingsPreferenceBaseline.request_timeout,300);assert.equal(settingsFormHydrating,false);
    """)


def test_read_newer_than_delayed_save_ack_is_not_rolled_back_by_the_old_ack():
    run_js(r"""
      await loaded();edit('#settingTimeout',300);const p=saveSettings({preventDefault(){}});
      // The server commits, but delivery of that response is delayed.
      backend={...backend,...posts[0].patch};const delayedAck=structuredClone(backend);
      await requestCloseWorkspaceDialog();backend.request_timeout=450;
      openWorkspacePanel('settings');await settle();assert.equal($('#settingTimeout').value,450);
      edit('#settingVoiceAutoSpeak',false);posts[0].resolve(delayedAck);await p;
      assert.equal($('#settingTimeout').value,450);assert.equal(settingsPreferenceBaseline.request_timeout,450);
      assert.equal($('#settingVoiceAutoSpeak').checked,false);assert.equal(settingsFormDirty,true);
      const next=saveSettings({preventDefault(){}});assert.deepEqual(posts[1].patch,{voice_auto_speak:false});posts[1].commit();await next;
    """)


def test_closed_save_failure_does_not_erase_new_visit_input_or_pretend_save_cancelled():
    run_js(r"""
      await loaded();edit('#settingTimeout',300);const p=saveSettings({preventDefault(){}});
      await requestCloseWorkspaceDialog();openWorkspacePanel('settings');await settle();
      edit('#settingTimeout',350);posts[0].reject(Error('Synthetic precommit failure'));await p;
      assert.equal(backend.request_timeout,120);assert.equal($('#settingTimeout').value,'350');
      assert.equal(settingsFormDirty,true);assert.equal(settingsPreferenceBaseline.request_timeout,120);
      assert(notices.at(-1)[0].includes('could not be confirmed'));assert.equal(settingsSavePending,false);
    """)


def test_same_visit_new_typing_is_merged_over_confirmed_remote_preferences():
    run_js(r"""
      await loaded();edit('#settingTimeout',300);const p=saveSettings({preventDefault(){}});
      edit('#settingTimeout',350);edit('#settingGithubToken','SYNTHETIC-LATER');
      backend.custom_system_prompt_suffix='Other entry';posts[0].commit();await p;
      assert.equal($('#settingTimeout').value,'350');assert.equal($('#settingGithubToken').value,'SYNTHETIC-LATER');
      assert.equal($('#settingCustomSystemPromptSuffix').value,'Other entry');assert.equal(settingsFormDirty,true);
      const next=saveSettings({preventDefault(){}});
      assert.deepEqual(posts[1].patch,{request_timeout:350,github_token:'SYNTHETIC-LATER'});posts[1].commit();await next;
    """)


def test_missing_baseline_and_loading_form_cannot_submit_a_stale_full_snapshot():
    run_js(r"""
      settingsPreferenceBaseline=null;await saveSettings({preventDefault(){}});assert.equal(posts.length,0);
      await loaded();settingsFormHydrating=true;await saveSettings({preventDefault(){}});
      assert.equal(posts.length,0);assert.equal(settingsSavePending,false);
    """)


def test_declining_pending_close_preserves_new_edits_and_does_not_cancel_submission():
    run_js(r"""
      await loaded();edit('#settingTimeout',300);const p=saveSettings({preventDefault(){}});
      edit('#settingTimeout',350);confirmDiscard=false;
      assert.equal(await requestCloseWorkspaceDialog(),false);assert.equal($('#workspaceDialog').open,true);
      assert.equal(settingsSavePending,true);posts[0].commit();await p;
      assert.equal(backend.request_timeout,300);assert.equal($('#settingTimeout').value,'350');
      assert.equal(settingsFormDirty,true);assert.equal(settingsPreferenceBaseline.request_timeout,300);
    """)


def test_save_while_closed_is_available_on_next_open_and_keeps_activation_warning():
    run_js(r"""
      await loaded();edit('#settingTimeout',300);const p=saveSettings({preventDefault(){}});
      await requestCloseWorkspaceDialog();posts[0].commit({saved:true,activation_warnings:[{component:'telegram',code:'activation_failed'}]});await p;
      assert.equal($('#workspaceDialog').open,false);assert(notices.at(-1)[0].includes('Telegram'));
      openWorkspacePanel('settings');await settle();assert.equal($('#settingTimeout').value,300);
      assert.equal(settingsFormDirty,false);assert($('#settingsSaveState').textContent.includes('Telegram'));
      assert.equal(settingsPreferenceBaseline.request_timeout,300);
    """)


def test_post_commit_refresh_failure_is_a_saved_warning_and_retains_new_visit_draft():
    run_js(r"""
      await loaded();edit('#settingTimeout',300);const p=saveSettings({preventDefault(){}});
      await requestCloseWorkspaceDialog();openWorkspacePanel('settings');await settle();
      edit('#settingTimeout',350);getDeferred=true;posts[0].commit();await settle();
      reads.at(-1).reject(Error('Synthetic refresh unavailable'));await p;
      assert.equal(backend.request_timeout,300);assert.equal($('#settingTimeout').value,'350');
      assert.equal(settingsFormDirty,true);assert.equal(settingsPreferenceBaseline.request_timeout,300);
      assert(notices.at(-1)[0].includes('Settings were saved'));assert.equal(notices.at(-1)[1],'info');
      assert(!$('#settingsSaveState').textContent.includes('Save failed'));
    """)


def test_provider_and_team_drafts_after_submission_survive_with_confirmed_baseline():
    run_js(r"""
      await loaded();providers=[{id:'stable-provider',provider:'openai',model:'synthetic',api_key:'SYNTHETIC-SUBMITTED'}];
      team=teamEntries('Submitted');markSettingsFormDirty();
      const p=saveSettings({preventDefault(){}});
      providers=[{...providers[0],api_key:'SYNTHETIC-NEWER'}];team=teamEntries('Newer');
      backend={...backend,...posts[0].patch,model_providers:[{...posts[0].patch.model_providers[0],api_key:''}]};
      posts[0].resolve(structuredClone(backend));await p;
      assert.equal(providers[0].api_key,'SYNTHETIC-NEWER');assert.equal(team[0].name,'Newer');
      assert.equal(settingsPreferenceBaseline.model_providers[0].api_key,'');
      assert.equal(settingsPreferenceBaseline.discussion_team[0].name,'Submitted');assert.equal(settingsFormDirty,true);
      const next=saveSettings({preventDefault(){}});
      assert.deepEqual(Object.keys(posts[1].patch).sort(),['discussion_team','model_providers']);posts[1].commit();await next;
    """)


def test_untouched_replacement_credential_is_not_resubmitted_with_a_new_list_edit():
    run_js(r"""
      await loaded();providers=[{id:'stable-provider',provider:'openai',model:'synthetic',api_key:'SYNTHETIC-SUBMITTED'}];
      const p=saveSettings({preventDefault(){}});
      providers.push({id:'new-provider',provider:'openai',model:'other',api_key:'SYNTHETIC-NEW'});
      backend={...backend,...posts[0].patch,model_providers:[{...posts[0].patch.model_providers[0],api_key:''}]};
      posts[0].resolve(structuredClone(backend));await p;
      assert.equal(providers[0].api_key,'');assert.equal(providers[1].api_key,'SYNTHETIC-NEW');
      const next=saveSettings({preventDefault(){}});assert.equal(posts[1].patch.model_providers[0].api_key,'');
      assert.equal(posts[1].patch.model_providers[1].api_key,'SYNTHETIC-NEW');posts[1].commit();await next;
    """)


def test_real_frontend_partial_patch_preserves_other_entry_in_actual_runtime_store(tmp_path, monkeypatch):
    import socket

    from deepdesk.runtime_settings import (
        RuntimeSettings,
        RuntimeSettingsPatch,
        RuntimeSettingsStore,
    )

    def refuse_network(*args, **kwargs):
        raise AssertionError("Network forbidden in settings regression")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    patch = run_js(r"""
      await loaded();edit('#settingVoiceAutoSpeak',false);const p=saveSettings({preventDefault(){}});
      const captured=posts[0].patch;posts[0].commit();await p;
    """, result_expression="captured")
    assert patch == {"voice_auto_speak": False}
    path = tmp_path / "synthetic-runtime-settings.json"
    store = RuntimeSettingsStore(path, RuntimeSettings(request_timeout=120))
    store.update(RuntimeSettingsPatch(request_timeout=300))
    result = store.update(RuntimeSettingsPatch.model_validate(patch))
    assert result.request_timeout == 300
    assert result.voice_auto_speak is False
    restarted = RuntimeSettingsStore(path, RuntimeSettings())
    assert restarted.value.request_timeout == 300
    assert restarted.value.voice_auto_speak is False
