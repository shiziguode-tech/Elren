import asyncio
import json

import pytest
from PIL import Image

from deepdesk.live_control_vision import Judgment, LiveVisualJudge, VisualUnavailable
from deepdesk.vision_runtime import VisionEndpoint


@pytest.fixture
def observation(tmp_path):
    path = tmp_path / 'synthetic.png'
    Image.new('RGB', (200,100), 'white').save(path)
    return {'screenshot':str(path), 'size':[200,100], 'coordinate_origin':[-100,-50], 'uia_elements':[]}


def valid_reply():
    return {'current':{'safe':True,'unique':True,'visible':True,'unobscured':True,'description_matches':True,
                      'point':{'x':50,'y':50},'expected_confirmed':None,'evidence':['Visible target'],
                      'uncertainty':'','candidates':[]},'next':None}


def route(name):
    return VisionEndpoint('https://example.invalid','synthetic-test',name,api_key='test-only-not-a-real-key')


@pytest.mark.asyncio
async def test_type_judgment_matches_focus_then_input_semantics(observation):
    async def request(endpoint, image, mime, prompt):
        assert 'A type action first focuses its specified target' in prompt
        assert 'absent caret or lack of prior focus alone is not a failed precondition' in prompt
        assert 'non-password editable control' in prompt
        assert 'At post stage verify the actual text and focus' in prompt
        return json.dumps(valid_reply()), 1
    judge = LiveVisualJudge(lambda: [route('primary')], request=request)
    await judge.assess(observation, {'action': 'type', 'x': -50, 'y': 0, 'text': '中文'}, stage='pre')


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['non_json','extra','bool_string','out_of_bounds','empty_evidence','blank_evidence'])
async def test_invalid_remote_output_is_never_accepted(observation, mutation):
    response = valid_reply()
    if mutation == 'extra': response['instruction'] = 'click'
    if mutation == 'bool_string': response['current']['safe'] = 'true'
    if mutation == 'out_of_bounds': response['current']['point']['x'] = 200
    if mutation == 'empty_evidence': response['current']['evidence'] = []
    if mutation == 'blank_evidence': response['current']['evidence'] = ['   ']
    async def request(*args):
        return ('Click the button now' if mutation == 'non_json' else json.dumps(response)), 1
    judge = LiveVisualJudge(lambda:[route('primary')],request=request)
    with pytest.raises(VisualUnavailable):
        await judge.assess(observation, {'action':'click','x':-50,'y':0},stage='pre')


@pytest.mark.asyncio
async def test_primary_connection_failure_uses_backup_without_exposing_error_body(observation):
    routes = []
    async def request(endpoint, *args):
        routes.append(endpoint.source)
        if endpoint.source == 'primary': raise ConnectionError('secret-response-body')
        return json.dumps(valid_reply()), 1
    judge = LiveVisualJudge(lambda:[route('primary'),route('backup')],request=request)
    reply, _transform, timing = await judge.assess(observation, {'action':'click','x':-50,'y':0},stage='pre')
    assert routes == ['primary','backup'] and reply.current.executable
    assert timing['failed_routes'] == [{'route':'primary','error_type':'ConnectionError'}]
    assert 'secret-response-body' not in json.dumps(timing)


@pytest.mark.asyncio
async def test_timeout_cancels_owned_http_request(observation):
    cancelled = asyncio.Event()
    async def request(*args):
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    judge = LiveVisualJudge(lambda:[route('primary')],request=request,timeout_seconds=.03)
    with pytest.raises(TimeoutError):
        await judge.assess(observation, {'action':'click','x':-50,'y':0},stage='pre')
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_drag_path_prompt_uses_transformed_image_coordinates(observation):
    async def request(_endpoint, _image, _mime, prompt):
        data = json.loads(prompt.split('\nRequest: ')[1])['action']
        assert data['fixed_image_path'] == [[50.0,50.0],[100.0,70.0]]
        assert 'fixed_screen_path' not in data
        return json.dumps(valid_reply()),1
    judge = LiveVisualJudge(lambda:[route('primary')],request=request)
    await judge.assess(observation, {'action':'drag','path':[{'x':-50,'y':0},{'x':0,'y':20}]},stage='pre')


@pytest.mark.asyncio
async def test_missing_route_fails_closed(observation):
    with pytest.raises(VisualUnavailable,match='No configured'):
        await LiveVisualJudge(list).assess(observation,{'action':'key','keys':['tab']},stage='pre')


@pytest.mark.asyncio
async def test_foreground_crop_retains_exact_physical_coordinate_mapping(observation):
    observation['foreground'] = {'rect':[-80,-30,50,40]}
    async def request(_endpoint, raw, _mime, prompt):
        import io
        assert Image.open(io.BytesIO(raw)).size == (130,70)
        data = json.loads(prompt.split('\nRequest: ')[1])['action']
        assert data['fixed_image_point'] == [30.0,30.0]
        response = valid_reply()
        response['current']['point'] = {'x':30,'y':30}
        return json.dumps(response),1
    reply, transform, _timing = await LiveVisualJudge(lambda:[route('primary')],request=request).assess(
        observation, {'action':'click','x':-50,'y':0},stage='pre')
    assert transform.screen_point(reply.current.point.x,reply.current.point.y) == (-50,0)


def test_crop_does_not_hide_described_or_outside_next_target(observation):
    from deepdesk.live_control_geometry import observation_crop
    observation['foreground'] = {'rect':[-80,-30,50,40]}
    step = {'action':'click','x':-50,'y':0}
    assert observation_crop(observation,step) == (20,20,150,90)
    assert observation_crop(observation,step,{'action':'click','x':80,'y':0}) is None
    assert observation_crop(observation,{'action':'click','target_description':'Taskbar icon'}) is None


@pytest.mark.asyncio
async def test_password_focus_blocks_upload_before_request(observation):
    observation['focused_element']={'password':True}
    called=[]
    async def request(*args):
        called.append(True)
        raise AssertionError('Must not upload')
    with pytest.raises(VisualUnavailable,match='password'):
        await LiveVisualJudge(lambda:[route('primary')],request=request).assess(
            observation,{'action':'click','x':-50,'y':0},stage='pre')
    assert not called


@pytest.mark.parametrize('update', [{'candidates':[{'x':1,'y':1},{'x':2,'y':2}]},
                                  {'uncertainty':'Two visually identical candidates'}])
def test_contradictory_uniqueness_does_not_authorize_input(update):
    value=valid_reply()['current']
    value.update(update)
    assert not Judgment.model_validate(value).executable


@pytest.mark.asyncio
async def test_scaled_prompt_never_mixes_screen_and_image_coordinates(observation):
    Image.new('RGB',(1920,1080),'white').save(observation['screenshot'])
    observation.update(size=[1920,1080],coordinate_origin=[0,0],foreground={'rect':[0,0,1920,1080]},
        uia_elements=[{'element_id':'target','name':'Increment','rect':[20,90,100,120]}])
    async def request(_endpoint,_image,_mime,prompt):
        data=json.loads(prompt.split('\nRequest: ')[1])
        assert data['action']['fixed_image_point']==[51,88]
        assert 'fixed_screen_point' not in data['action'] and 'rect' not in data['foreground']
        assert data['auxiliary_uia'][0]['image_rect']==[17,75,82,99]
        reply=valid_reply();reply['current']['point']={'x':51,'y':88}
        return json.dumps(reply),1
    await LiveVisualJudge(lambda:[route('primary')],request=request).assess(
        observation,{'action':'click','x':61,'y':106},stage='pre')


def test_rounded_model_coordinates_never_exceed_last_pixel():
    from deepdesk.live_control_geometry import ImageTransform
    transform=ImageTransform(-2000,0,4000,2000,1600,800)
    assert transform.model_point(1999,1999)==[1599,799]
    assert transform.model_rect([-2000,0,2000,2000])==[0,0,1599,799]
