import asyncio
import threading

import pytest
from test_live_computer_use import (
    live_fixture as live_fixture,  # noqa: PLC0414 -- register pytest fixture
)

from deepdesk.live_control_geometry import ImageTransform
from deepdesk.live_control_vision import Judgment, VisualReply, VisualUnavailable


class StubJudge:
    def __init__(self, fail_stage=None, expected=True, safe=True):
        self.calls = []
        self.fail_stage, self.expected, self.safe = fail_stage, expected, safe

    async def assess(self, observation, action, *, stage, next_action=None):
        self.calls.append(stage)
        if self.fail_stage == stage:
            raise VisualUnavailable('test unavailable')
        judgment = Judgment(safe=self.safe, unique=True, visible=True, unobscured=True,
            description_matches=True, point=None, expected_confirmed=self.expected if stage=='post' else None,
            evidence=['Controlled fixture target visible'], uncertainty='', candidates=[])
        transform = ImageTransform(*observation['coordinate_origin'], *observation['size'], *observation['size'])
        return VisualReply(current=judgment, next=judgment if next_action else None), transform, {'remote_vision_ms': 0}


async def started_fixture(live_fixture, judge):
    tool, _controller, context, _screen, driver, _foreground = live_fixture
    tool.visual_flow.judge = judge
    started = await tool.execute({'action':'start'}, context)
    arguments = {'action': 'click', 'x': -50, 'y': 0, 'action_id':'call-1',
        'session_lease':started['session_lease'], 'observation_id': started['observation']['observation_id']}
    return tool, context, driver, arguments


@pytest.mark.asyncio
@pytest.mark.parametrize('restores', [True, False])
async def test_caret_phase_requires_exact_original_pixels(live_fixture, restores):
    tool, _controller, context, screen, *_ = live_fixture
    start = await tool.execute({'action':'start'}, context)
    original = screen.image.copy()
    for y in range(10, 27):
        screen.image.putpixel((3, y), (0,0,0))
    async def blink():
        await asyncio.sleep(.16)
        screen.image = original
    task = asyncio.create_task(blink()) if restores else None
    try:
        if restores:
            assert await tool.visual_flow.valid(start['session_lease'], context, start['observation']) > 0
        else:
            from deepdesk.plugins.builtin.live_computer_use import StaleObservationError
            with pytest.raises(StaleObservationError):
                await tool.visual_flow.valid(start['session_lease'], context, start['observation'])
    finally:
        if task:
            await task


@pytest.mark.asyncio
async def test_caret_phase_retry_does_not_hide_external_input(live_fixture, monkeypatch):
    tool, controller, context, *_ = live_fixture
    start = await tool.execute({'action':'start'}, context)
    from deepdesk.plugins.builtin.live_computer_use import StaleObservationError
    calls=[]
    def check(*args):
        calls.append(1)
        if len(calls) == 1:
            error=StaleObservationError('caret')
            error.visual_change_box=(3,10,4,27)
            raise error
        raise InterruptedError('external input')
    monkeypatch.setattr(controller,'validate_visual_observation',check)
    with pytest.raises(InterruptedError):
        await tool.visual_flow.valid(start['session_lease'],context,start['observation'])
    assert len(calls)==2


@pytest.mark.asyncio
async def test_remote_checks_and_duplicate_input_suppression(live_fixture):
    judge = StubJudge()
    tool, context, driver, arguments = await started_fixture(live_fixture, judge)
    result = await tool.execute(arguments, context)
    assert result['status'] == 'delivered_unconfirmed'
    assert result['effect_verified'] is False
    assert judge.calls == ['pre', 'post']
    assert len(driver.clicks) == 1
    duplicate = await tool.execute(arguments, context)
    assert duplicate['duplicate'] is True
    assert len(driver.clicks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stage,clicks,status', [('pre',0,'rejected'),('post',1,'outcome_unknown')])
async def test_visual_failure_never_replays_input(live_fixture,stage,clicks,status):
    tool,context,driver,args = await started_fixture(live_fixture,StubJudge(fail_stage=stage))
    result=await tool.execute(args,context)
    assert len(driver.clicks)==clicks
    assert result['status']==status
    await tool.execute(args,context)
    assert len(driver.clicks)==clicks


@pytest.mark.asyncio
async def test_sequence_shares_post_pre_check(live_fixture):
    judge=StubJudge()
    tool,context,driver,args=await started_fixture(live_fixture,judge)
    args.update(action='sequence',actions=[{'action':'click','x':-50,'y':0}, {'action':'click','x':-30,'y':20}])
    result=await tool.execute(args,context)
    assert len(driver.clicks)==2
    assert judge.calls==['pre','post','post']
    assert result['stopped_step'] is None
    assert result['steps'][1]['check_reuses']==1


@pytest.mark.asyncio
async def test_unconfirmed_expected_stops_remaining_steps(live_fixture):
    tool,context,driver,args=await started_fixture(live_fixture,StubJudge(expected=False))
    args.update(action='sequence',actions=[{'action':'click','x':-50,'y':0,'expected_result':'Dialog opens'}, {'action':'click','x':-30,'y':20}])
    result=await tool.execute(args,context)
    assert len(driver.clicks)==1
    assert result['stopped_step']==0


@pytest.mark.asyncio
async def test_user_interference_rejects_even_with_good_model(live_fixture):
    judge=StubJudge()
    tool,context,driver,args=await started_fixture(live_fixture,judge)
    driver.pointer[:]=[20,20]
    result=await tool.execute(args,context)
    assert result['status']=='interrupted'
    assert not driver.clicks and not judge.calls


@pytest.mark.asyncio
async def test_conflicting_duplicate_id_rejected(live_fixture):
    tool,context,driver,args=await started_fixture(live_fixture,StubJudge())
    await tool.execute(args,context)
    with pytest.raises(ValueError,match='different arguments'):
        await tool.execute({**args,'x':-40},context)
    assert len(driver.clicks)==1


def test_coordinate_transform_crop_scale_negative_monitor():
    transform=ImageTransform(-1920,-100,1000,500,500,250,100,50)
    assert transform.screen_point(250,125)==(-1320,200)
    assert transform.image_point(-1320,200)==(250,125)
    with pytest.raises(ValueError):transform.screen_point(500,0)
    with pytest.raises(ValueError):transform.screen_point(float('nan'),0)


def test_css_transform_requires_measured_scale():
    assert ImageTransform.css_point(20,30,(-100,100),1.5)==(-70,145)
    with pytest.raises(ValueError):ImageTransform.css_point(20,30,(0,0),0)


@pytest.mark.asyncio
async def test_stop_cleans_observe_only_session(live_fixture):
    tool, context, _driver, _args = await started_fixture(live_fixture, StubJudge())
    assert tool.visual_flow.observations
    await tool.cleanup(context)
    assert not tool.visual_flow.observations
    assert not tool.visual_flow.owners


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_step', [
    {'action':'type', 'text':12}, {'action':'scroll', 'scroll_y':True},
    {'action':'drag', 'path':[{'x':0,'y':0}], 'duration':1},
    {'action':'key', 'keys':['escape']}, {'action':'click','x':0},
])
async def test_invalid_later_step_never_partially_executes_sequence(live_fixture, bad_step):
    judge = StubJudge()
    tool, context, driver, args = await started_fixture(live_fixture, judge)
    args.update(action='sequence', actions=[{'action':'click','x':-50,'y':0}, bad_step])
    with pytest.raises(ValueError):
        await tool.execute(args, context)
    assert not driver.clicks and not judge.calls


class WaitingJudge(StubJudge):
    def __init__(self):
        super().__init__()
        self.entered, self.cancelled = asyncio.Event(), asyncio.Event()

    async def assess(self, *args, **kwargs):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


@pytest.mark.asyncio
async def test_cancellation_owns_remote_request_and_never_sends_input(live_fixture):
    judge = WaitingJudge()
    tool, context, driver, args = await started_fixture(live_fixture, judge)
    call = asyncio.create_task(tool.execute(args, context))
    await asyncio.wait_for(judge.entered.wait(), 2)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert judge.cancelled.is_set()
    assert not tool.controller.public_status()['active']
    assert not driver.clicks


@pytest.mark.asyncio
async def test_focus_change_cancels_waiting_model_and_sequence(live_fixture):
    judge = WaitingJudge()
    tool, context, driver, args = await started_fixture(live_fixture, judge)
    call = asyncio.create_task(tool.execute(args, context))
    await asyncio.wait_for(judge.entered.wait(), 2)
    live_fixture[-1]['focus'] = {'window_handle':999}
    result = await asyncio.wait_for(call, 2)
    assert result['status'] == 'interrupted'
    assert result['visual_calls'] == 1
    assert judge.cancelled.is_set() and not driver.clicks


@pytest.mark.asyncio
async def test_hide_returned_observation_does_not_skip_internal_visual_checks(live_fixture):
    judge = StubJudge()
    tool, context, driver, args = await started_fixture(live_fixture, judge)
    result = await tool.execute({**args,'return_screenshot':False,'return_elements':False},context)
    assert judge.calls == ['pre','post']
    assert 'screenshot' not in result['observation'] and 'uia_elements' not in result['observation']
    assert 'screenshot' in tool.visual_flow.observations[args['session_lease']]
    assert len(driver.clicks) == 1


@pytest.mark.asyncio
async def test_repeated_cancel_waits_until_admitted_input_quiesces(live_fixture):
    tool, context, driver, args = await started_fixture(live_fixture, StubJudge())
    entered, release = threading.Event(), threading.Event()
    original_click = driver.click
    def blocking_click(*values, **kwargs):
        entered.set()
        assert release.wait(3)
        original_click(*values, **kwargs)
    driver.click = blocking_click
    call = asyncio.create_task(tool.execute(args, context))
    assert await asyncio.to_thread(entered.wait, 2)
    call.cancel()
    await asyncio.sleep(.03)
    call.cancel()
    await asyncio.sleep(.03)
    assert not call.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not tool.controller.public_status()['active']
    count = len(driver.clicks)
    await asyncio.sleep(.1)
    assert len(driver.clicks) == count == 1


def test_quiesce_retries_failed_stop_without_releasing_ownership(live_fixture, monkeypatch):
    _tool, controller, context, *_ = live_fixture
    controller.start(context.task_id, 600, 'en')
    original_stop = controller.emergency_stop
    calls = []
    def transient_stop(reason, **kwargs):
        calls.append(reason)
        return False if len(calls) == 1 else original_stop(reason, **kwargs)
    monkeypatch.setattr(controller, 'emergency_stop', transient_stop)
    controller.quiesce_task(context.task_id, 'test_cancel')
    assert calls == ['test_cancel', 'test_cancel']
    assert not controller.public_status()['active']


@pytest.mark.asyncio
async def test_complete_unicode_text_uses_two_visual_checks_not_per_character(live_fixture, monkeypatch):
    import deepdesk.plugins.builtin.live_computer_use as live_module
    judge = StubJudge()
    tool, context, _driver, args = await started_fixture(live_fixture, judge)
    typed = []
    monkeypatch.setattr(live_module, 'type_unicode', typed.append)
    text = '中文 English 混合输入🙂 ' * 10
    result = await tool.execute({**args, 'action':'type', 'text':text}, context)
    assert ''.join(typed) == text
    assert judge.calls == ['pre','post']
    assert result['status'] == 'delivered_unconfirmed'


@pytest.mark.asyncio
async def test_programmatic_focus_change_stops_remaining_text_chunks(live_fixture, monkeypatch):
    import deepdesk.plugins.builtin.live_computer_use as live_module
    tool, context, _driver, args = await started_fixture(live_fixture, StubJudge())
    typed = []
    def change_focus(chunk):
        typed.append(chunk)
        live_fixture[-1]['focus'] = {'window_handle':999}
    monkeypatch.setattr(live_module, 'type_unicode', change_focus)
    result = await tool.execute({**args, 'action':'type', 'text':'中' * 100}, context)
    assert len(''.join(typed)) == 32
    assert result['status'] == 'interrupted'
    assert result['input_may_have_been_sent'] is True
    assert not tool.controller.public_status()['active']


@pytest.mark.asyncio
async def test_same_hwnd_different_uia_focus_interrupts_remote_wait(live_fixture, monkeypatch):
    judge=WaitingJudge()
    tool, context, driver, args=await started_fixture(live_fixture,judge)
    call=asyncio.create_task(tool.execute(args,context))
    await asyncio.wait_for(judge.entered.wait(),2)
    monkeypatch.setattr(tool.controller,'_focused_element',lambda:{'runtime_id':[1,2]})
    result=await asyncio.wait_for(call,2)
    assert result['status']=='interrupted'
    assert judge.cancelled.is_set() and not driver.clicks


@pytest.mark.asyncio
async def test_pixels_change_during_uia_collection_publish_fresh_visual_only_frame(live_fixture, monkeypatch):
    tool, controller, context, screen, *_=live_fixture
    def moving_elements(_foreground):
        screen.change(12,12)
        return [{'element_id':'old-position'}], {'old-position':{}}
    monkeypatch.setattr(controller,'_inspect_uia',moving_elements)
    result=await tool.execute({'action':'start'},context)
    observation=result['observation']
    assert observation['uia_elements']==[]
    assert observation['uia_discarded_reason']
    assert controller._session.observation.uia_targets=={}
    assert controller._session.observation.image.tobytes()==screen.image.tobytes()


@pytest.mark.asyncio
@pytest.mark.parametrize('continuous', [False,True])
async def test_pre_model_layout_changes_have_one_localization_retry_not_input_redo(live_fixture, continuous):
    screen=live_fixture[3]
    class MovingJudge(StubJudge):
        async def assess(self, *args, **kwargs):
            reply=await super().assess(*args,**kwargs)
            if kwargs['stage']=='pre' and (len(self.calls)==1 or continuous):
                screen.image.putpixel((3+len(self.calls),3),(0,0,0))
            return reply
    judge=MovingJudge()
    tool,context,driver,args=await started_fixture(live_fixture,judge)
    args['target_description']='Same target at the exact requested point'
    result=await tool.execute(args,context)
    assert result['relocations']==1
    assert len(driver.clicks)==(0 if continuous else 1)
    assert judge.calls==(['pre','pre'] if continuous else ['pre','pre','post'])
    assert result['action_redos']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('continuous', [False, True])
async def test_post_model_layout_retry_observes_without_replaying_input(live_fixture, continuous):
    screen = live_fixture[3]

    class MovingPostJudge(StubJudge):
        async def assess(self, *args, **kwargs):
            reply = await super().assess(*args, **kwargs)
            if kwargs['stage'] == 'post' and (self.calls.count('post') == 1 or continuous):
                screen.image.putpixel((3 + len(self.calls), 3), (0, 0, 0))
            return reply

    judge = MovingPostJudge()
    tool, context, driver, args = await started_fixture(live_fixture, judge)
    args['expected_result'] = 'Controlled target changed'
    result = await tool.execute(args, context)
    assert len(driver.clicks) == 1
    assert judge.calls == ['pre', 'post', 'post']
    assert result['post_observation_retries'] == 1
    assert result['action_redos'] == 0
    assert result['status'] == ('outcome_unknown' if continuous else 'expected_confirmed')
