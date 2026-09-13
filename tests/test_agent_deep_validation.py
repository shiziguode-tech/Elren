import json

from deepdesk.harness import select_tool_schemas
from deepdesk.models import AgentProfile, AgentTask
from test_context_compaction import _engine


def schema(name, size):
    return {'type':'function','function':{'name':name,'description':'x'*size,
        'parameters':{'type':'object','properties':{}}}}


def test_explicitly_discovered_tool_is_not_starved_by_profile_defaults():
    catalog=[schema(name, 2400) for name in
             ['filesystem','shell','background_browser','wanted_tool']]
    visible, state=select_tool_schemas(catalog, 'Continue', AgentProfile.CODER,
        activated_names=['wanted_tool'],character_budget=6000)
    assert state['deferred']
    assert 'wanted_tool' in [item['function']['name'] for item in visible]


def test_current_request_survives_repeated_compaction_with_old_history(tmp_path):
    engine=_engine(tmp_path,type('Client',(),{'keys':[]})())
    task=AgentTask(prompt='Only inspect the parser. Do not modify files. CURRENT-CONSTRAINT-719',
        context_prompt='Old task: build website and publish. '+('historical detail '*8000))
    # Also cover a valid combined context that DOES contain the latest request,
    # but places it in the middle of a long history that middle-compaction cuts.
    task.context_prompt = ('Old task: build website and publish. ' + 'historical detail '*4000
                           + task.prompt + 'historical detail '*4000)
    messages=[{'role':'system','content':'system'}, {'role':'user','content':task.context_prompt}]
    ledger=[]
    archives=[]
    for index in range(1,4):
        messages.append({'role':'tool','content':'untrusted historical data '*3000})
        messages, info=engine._compact_context_messages(task,messages,checkpoint_number=index,
            reason='test',cumulative_ledger=ledger,checkpoint_archives=archives,
            runtime_state={},target_tokens=3000,active_schemas=[])
        assert task.prompt in messages[1]['content']
        assert all(path in messages[1]['content'] for path in archives)
        assert json.loads(open(info['archive'],encoding='utf-8').read())['task_id']==task.id
