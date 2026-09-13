import json

import pytest

from deepdesk.model_capabilities import is_retired_deepseek_selector
from deepdesk.runtime_settings import RuntimeSettingsStore


@pytest.mark.parametrize('selector',[
    'deepseek-v4.1-flash-expires-on-0910',
    ' DEEPSEEK-V4.1-FLASH-EXPIRES-ON-0910 ',
    'deepseek:deepseek-v4.1-flash-expires-on-0910',
])
def test_retired_selector_detection(selector):
    assert is_retired_deepseek_selector(selector)


@pytest.mark.parametrize('selector',[
    'deepseek-v4-flash','deepseek-v4-flash-vision-exp','deepseek-v4-pro',
    'custom:deepseek-v4.1-flash-expires-on-0910','auto',
])
def test_other_routes_are_not_retired(selector):
    assert not is_retired_deepseek_selector(selector)


def test_old_disk_defaults_migrate_without_touching_chat_history(tmp_path):
    from deepdesk.runtime_settings import RuntimeSettings, RuntimeSettingsPatch
    path=tmp_path/'runtime-settings.json'
    path.write_text(json.dumps({'model':'deepseek-v4.1-flash-expires-on-0910',
                               'reasoning_effort':'max','theme_mode':'dark'}),'utf-8')
    history=tmp_path/'history.json'
    old=b'{"model":"deepseek-v4.1-flash-expires-on-0910","text":"historical"}'
    history.write_bytes(old)
    store=RuntimeSettingsStore(path,RuntimeSettings())
    assert store.value.model=='auto'
    assert store.value.theme_mode=='dark' and store.value.reasoning_effort=='max'
    store.update(RuntimeSettingsPatch(model='deepseek-v4.1-flash-expires-on-0910'))
    assert json.loads(path.read_text('utf-8'))['model']=='auto'
    assert history.read_bytes()==old


def test_frontend_retired_selector_is_exact_and_alias_aware():
    from test_frontend_high_priority import _run_javascript
    result=_run_javascript("['deepseek-v4.1-flash-expires-on-0910','deepseek:deepseek-v4.1-flash-expires-on-0910','custom:deepseek-v4.1-flash-expires-on-0910','deepseek-v4-flash'].map(isRetiredBuiltinModel)", 'isRetiredBuiltinModel')
    assert result==[True,True,False,False]
