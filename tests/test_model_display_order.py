import json
import shutil
import subprocess
from pathlib import Path

import pytest

from deepdesk.deepseek import AICODEMIRROR_MODELS, DeepSeekClient

ROOT = Path(__file__).resolve().parents[1]


def _function(name):
    source = (ROOT / "deepdesk/static/app.js").read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    return source[start:source.index("\nfunction ", start + 1)]


def _javascript(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for frontend behavior verification")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True,
                            encoding="utf-8", check=True, timeout=15)
    return json.loads(result.stdout)


def test_display_order_moves_astra_without_mutating_routing_catalog():
    result = _javascript(_function("modelOptionsForDisplay") + """
const model = (provider, name) => Object.freeze({provider, model:name, selector:provider+':'+name});
const original = Object.freeze([
  model('deepseek','deepseek-v4-flash'),
  model('aicodemirror-openai','gpt-5.6-sol'),
  model('aicodemirror-openai','gpt-5.6-terra'),
  model('aicodemirror-openai','gpt-6-astra'),
  model('openai','gpt-5.6-sol'),
  model('openai','gpt-6-astra'),
  model('aicodemirror-google','gemini-3.8-flash'),
]);
const ordered = modelOptionsForDisplay(original);
process.stdout.write(JSON.stringify({
  original:original.map(x=>x.model),
  order:ordered.map(x=>x.model),
  sameObjects:ordered.every(x=>original.includes(x)),
  idempotent:JSON.stringify(ordered)===JSON.stringify(modelOptionsForDisplay(ordered)),
  noSol:modelOptionsForDisplay([original[3],original[6]]).map(x=>x.model),
  empty:modelOptionsForDisplay([]),
}));
""")
    assert result["original"][1:4] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra"]
    assert result["order"] == ["deepseek-v4-flash", "gpt-6-astra", "gpt-5.6-sol",
                               "gpt-5.6-terra", "gpt-6-astra", "gpt-5.6-sol",
                               "gemini-3.8-flash"]
    assert result["sameObjects"] and result["idempotent"]
    assert result["noSol"] == ["gpt-6-astra", "gemini-3.8-flash"]
    assert result["empty"] == []


def test_selector_rebuild_preserves_model_and_reasoning_preferences():
    result = _javascript(
        _function("isRetiredBuiltinModel") + _function("modelOptionsForDisplay") + _function("syncModelSelectors") + """
const nodes = {
  '#modelPreference':{value:'aicodemirror-openai:gpt-5.6-terra'},
  '#settingModel':{value:'aicodemirror-openai:gpt-5.6-sol'},
  '#settingReasoningEffort':{value:'max'},
};
const $ = name => nodes[name];
let availableModelOptions=[], automaticDefaultModelSelector='', defaultModelSelector='';
let pendingLanguageSwitchModel='', discussionTeamConfigured=false;
let discussionTeamLeaderModel='auto', currentTaskSnapshot=null;
let settingModelDirty=true, settingReasoningDirty=true;
let settingsPreferenceBaseline=null, settingsFormBaseline=null;
const uiText=(zh,en)=>en, escapeHtml=value=>value;
const modelDisplayName=selector=>selector;
const refreshDiscussionTeamModelOptions=()=>{}, refreshModelPreferenceUI=()=>{};
let reasoningArgs;
const syncDefaultReasoningOptions=(...args)=>{reasoningArgs=args;};
const syncReasoningAvailability=()=>{}, isBlankNewTaskComposer=()=>false;
const options = ['gpt-5.6-sol','gpt-5.6-terra','gpt-6-astra'].map(model=>({
  provider:'aicodemirror-openai',model,selector:'aicodemirror-openai:'+model,
}));
syncModelSelectors({available_models:options,model:'auto',reasoning_effort:'high',
                    active_model:'aicodemirror-openai:gpt-5.6-terra'});
process.stdout.write(JSON.stringify({
  composer:nodes['#modelPreference'].value,
  defaultModel:nodes['#settingModel'].value,
  reasoningArgs, availableOrder:availableModelOptions.map(x=>x.model),
  sameCatalog:availableModelOptions===options,
  markup:nodes['#modelPreference'].innerHTML,
  defaultMarkup:nodes['#settingModel'].innerHTML,
}));
""")
    assert result["composer"] == "aicodemirror-openai:gpt-5.6-terra"
    assert result["defaultModel"] == "aicodemirror-openai:gpt-5.6-sol"
    assert result["reasoningArgs"] == ["aicodemirror-openai:gpt-5.6-sol", "max"]
    assert result["sameCatalog"]
    assert result["availableOrder"] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra"]
    for markup in [result["markup"], result["defaultMarkup"]]:
        assert markup.index("gpt-6-astra") < markup.index("gpt-5.6-sol")


def test_only_display_order_changes_and_cache_version_is_updated():
    assert AICODEMIRROR_MODELS["openai"][:3] == (
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra",
    )
    client = DeepSeekClient("https://example.invalid", "deepseek-v4-flash", "", "")
    client.set_provider_models([], {"openai": "test-only"})
    assert client.select_automatic_model(False) == "aicodemirror-openai:gpt-5.6-terra"
    assert client.select_automatic_model(True, "code") == "aicodemirror-openai:gpt-5.6-sol"
    assert "modelOptionsForDisplay(availableModelOptions).map" in _function(
        "discussionTeamModelOptions"
    )
    assert '/static/app.js?v=264' in (ROOT / "deepdesk/static/index.html").read_text("utf-8")
