"""Bounded action executor. It never plans tasks or adds business steps."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
from collections import OrderedDict

from deepdesk.live_control_geometry import observation_crop, transform_box
from deepdesk.live_control_vision import LiveVisualJudge

ACTIONS = frozenset({'click', 'double_click', 'type', 'key', 'scroll', 'drag'})


def action_fingerprint(action):
    return hashlib.sha256(json.dumps({k: v for k, v in action.items()
        if k not in {'action_id', 'session_lease', 'return_screenshot', 'return_elements'}},
        sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_action(action):
    """Validate every sequence item before admitting any native input."""
    if not isinstance(action, dict) or action.get('action') not in ACTIONS:
        raise ValueError('Unsupported action')
    if ('x' in action or 'y' in action) and not all(
        type(action.get(k)) is int for k in ('x', 'y')
    ):
        raise ValueError('Coordinates require integer x and y')
    for name in ('target_description', 'expected_result'):
        if name in action and (not isinstance(action[name], str) or len(action[name]) > 2000):
            raise ValueError(f'{name} must be a string of at most 2000 characters')
    if action.get('button', 'left') not in {'left', 'middle', 'right'}:
        raise ValueError('Unsupported mouse button')
    kind = action['action']
    if kind == 'type' and (not isinstance(action.get('text'), str) or len(action['text']) > 8000):
        raise ValueError('type requires a string of at most 8000 characters')
    if kind == 'key':
        keys = action.get('keys')
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8 or any(not isinstance(k, str) or not k.strip() for k in keys):
            raise ValueError('key requires 1–8 nonempty key names')
        if any(k.strip().casefold() in {'esc', 'escape'} for k in keys):
            raise ValueError('Esc is reserved for local emergency stop')
    if kind == 'scroll' and any(
        type(action.get(k, 0)) is not int or abs(action.get(k, 0)) > 12000
        for k in ('scroll_x', 'scroll_y')
    ):
        raise ValueError('Scroll offsets must be integer pixels within ±12000')
    if kind == 'drag':
        path, duration = action.get('path'), action.get('duration', .6)
        if not isinstance(path, list) or not 2 <= len(path) <= 64 or any(
                not isinstance(p, dict) or not all(type(p.get(k)) is int for k in ('x', 'y')) for p in path):
            raise ValueError('drag requires 2–64 integer coordinate points')
        if type(duration) not in (int, float) or not math.isfinite(duration) or not .1 <= duration <= 5:
            raise ValueError('drag duration must be between .1 and 5 seconds')


class LiveActionFlow:
    def __init__(self, tool, judge: LiveVisualJudge):
        self.tool, self.judge = tool, judge
        self.lock = asyncio.Lock()
        self.observations = {}
        self.owners = {}
        self.receipts = OrderedDict()
        self.cache = {}

    def remember(self, lease, observation, task_id):
        self.observations[lease] = observation
        self.owners[lease] = task_id

    def release_task(self, task_id):
        leases = {lease for lease, owner in self.owners.items() if owner == task_id}
        leases.update(key[1] for key in self.receipts if key[0] == task_id)
        for key in list(self.receipts):
            if key[0] == task_id:
                del self.receipts[key]
        for lease in leases:
            self.observations.pop(lease, None)
            self.cache.pop(lease, None)
            self.owners.pop(lease, None)

    def metrics(self, task_id):
        records = [r for (task, _lease, _id), (_hash, r) in self.receipts.items() if task == task_id]
        steps = [s for r in records for s in r.get('steps', [])]
        def distribution(values):
            values = sorted(values)
            return {'count': len(values), 'p50': values[max(0, math.ceil(len(values)*.5)-1)] if values else None,
                    'p95': values[max(0, math.ceil(len(values)*.95)-1)] if values else None}
        return {'measurement_scope': 'tool-call latency, not main-agent decision speed',
                'action_ms': distribution([s['total_ms'] for s in steps if 'total_ms' in s]),
                'call_ms': distribution([r['total_ms'] for r in records if 'total_ms' in r]),
                'visual_calls': sum(s.get('visual_calls', 0) for s in steps),
                'check_reuses': sum(s.get('check_reuses', 0) for s in steps),
                'relocations': sum(s.get('relocations', 0) for s in steps),
                'action_redos': 0,
                'expected_confirmed': sum(s.get('expected_confirmed', False) for s in steps),
                'outcome_unknown': sum(s.get('status') == 'outcome_unknown' for s in steps),
                'rejected': sum(s.get('status') == 'rejected' for s in steps),
                'interrupted': sum(s.get('status') == 'interrupted' for s in steps)}

    @staticmethod
    def present(receipt, arguments):
        value = copy.deepcopy(receipt)
        observation = value.get('observation')
        if observation:
            if arguments.get('return_screenshot') is False:
                observation.pop('screenshot', None)
                observation.pop('screenshot_url', None)
            if arguments.get('return_elements') is False:
                observation.pop('uia_elements', None)
                observation.pop('ocr', None)
        return value

    async def valid(self, lease, context, observation, box=None):
        # A blinking caret can differ in phase from the model's frame. Never
        # mask pixels or accept a merely small difference: wait briefly for an
        # EXACT match, rechecking input, focus, lease and geometry each time.
        deadline = time.monotonic() + 1.2
        resamples = 0
        while True:
            try:
                await self.tool._controller_call(self.tool.controller.validate_visual_observation,
                    lease, context.task_id, observation['observation_id'], box)
                return resamples
            except Exception as exc:
                region = getattr(exc, 'visual_change_box', None)
                narrow = (isinstance(region, (tuple, list)) and len(region) == 4
                          and 0 < region[2]-region[0] <= 3 and 0 < region[3]-region[1] <= 64)
                if type(exc).__name__ != 'StaleObservationError' or not narrow or time.monotonic() >= deadline:
                    raise
                resamples += 1
                await asyncio.sleep(0.08)

    async def refresh_same_target(self, lease, context, observation, action):
        """One pre-input relocation, never an action redo or changed goal."""
        try:
            await self.valid(lease, context, observation, observation_crop(observation, self.bind(action, observation)))
            return observation, action, 0
        except Exception as exc:
            if type(exc).__name__ != 'StaleObservationError':
                raise  # External input/focus changes must pause, not recover.
        old_element = None
        if action.get('element_id'):
            old_element = next((e for e in observation.get('uia_elements', [])
                                if e['element_id']==action['element_id']), None)
            if old_element is None or not old_element.get('automation_id'):
                raise ValueError('Stale element has no stable identity; caller must reselect')
        elif not action.get('target_description'):
            raise ValueError('Stale target has no identity evidence; call observe')
        # Snapshot fresh state once; model must judge it before any input.
        fresh = await self.tool._observe(lease, context, self.tool._select_ocr_language_tags([], context.user_prompt))
        rebound = dict(action)
        if old_element:
            keys = ('automation_id', 'name', 'control_type')
            matches = [e for e in fresh.get('uia_elements', [])
                       if all(e.get(k)==old_element.get(k) for k in keys) and e.get('enabled')]
            if len(matches) != 1:
                raise ValueError('Same element identity is missing or ambiguous after relocation')
            rebound['element_id'] = matches[0]['element_id']
        # If explicit x/y were supplied, bind() will still keep them unchanged.
        return fresh, rebound, 1

    async def judge_alive(self, lease, context, observation, action, **kwargs):
        """Cancel the owned remote request promptly after Esc/session stop."""
        request = asyncio.create_task(self.judge.assess(observation, action, **kwargs))
        try:
            while not request.done():
                await asyncio.wait({request}, timeout=0.1)
                status = self.tool.controller.public_status()
                if not status.get('active'):
                    raise InterruptedError('Control session stopped during visual request')
                await self.tool._controller_call(self.tool.controller.validate_remote_wait,
                    lease, context.task_id, observation['observation_id'])
            result = await request
            resamples = await self.valid(lease, context, observation, transform_box(result[1]))
            reply, transform, timing = result
            return reply, transform, {**timing, 'validity_phase_resamples': resamples}
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    def bind(self, action, observation):
        bound = {k: v for k, v in action.items() if not k.startswith('_')}
        # Explicit coordinates remain exact even when an accessible element is
        # under the point. Never silently replace them with an element center.
        if 'x' in bound or 'y' in bound:
            if not all(type(bound.get(k)) is int for k in ('x', 'y')):
                raise ValueError('Coordinates require integer x and y')
            bound.pop('element_id', None)
            bound['_visual_fixed_coordinate'] = True
        elif bound.get('element_id'):
            matches = [e for e in observation.get('uia_elements', []) if e['element_id']==bound['element_id']]
            if len(matches) != 1:
                raise ValueError('Element does not belong to this observation')
            element = matches[0]
            if not element.get('enabled'):
                raise ValueError('Target element is disabled')
            rect = element['rect']
            bound['x'], bound['y'] = (rect[0]+rect[2])//2, (rect[1]+rect[3])//2
            bound.setdefault('target_description', element.get('name') or element.get('control_type'))
            bound['_identity'] = {k: element.get(k) for k in ('automation_id', 'name', 'control_type')}
        return bound

    async def run(self, arguments, context):
        lease = str(arguments.get('session_lease') or '')
        # Serialize full calls, not just individual input events. Stop remains
        # outside this lock and can always cancel the controller lease.
        async with self.lock:
            await self.tool._controller_call(self.tool.controller.status_with_lease, lease, context.task_id)
            fingerprint = action_fingerprint(arguments)
            request_id = arguments.get('action_id') or ('implicit-' + fingerprint)
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise ValueError('action_id must be a 1–128 character string')
            key = (context.task_id, lease, request_id)
            if key in self.receipts:
                old_hash, receipt = self.receipts[key]
                if old_hash != fingerprint:
                    raise ValueError('action_id was already used for different arguments')
                return {**self.present(receipt, arguments), 'duplicate': True}
            if len(self.receipts) >= 512:
                raise ValueError('Receipt capacity reached; start a new tool instance rather than risk duplicate input')
            sequence = arguments.get('action') == 'sequence'
            steps = arguments.get('actions') if sequence else [arguments]
            if not isinstance(steps, list) or not 1 <= len(steps) <= 16:
                raise ValueError('Sequence requires 1–16 explicit actions')
            for step in steps:
                validate_action(step)
                self.tool._validate_sensitive_key_action(step['action'], step, context)
            receipt = {'action_id': request_id, 'status': 'not_executed', 'input_delivered': False,
                       'steps': [], 'stopped_step': 0, 'duplicate': False}
            self.receipts[key] = (fingerprint, receipt)
            started = time.perf_counter()
            observation = self.observations.get(lease)
            if observation is None or arguments.get('observation_id') != observation.get('observation_id'):
                receipt.update(reason='Missing or stale observation; call observe', status='rejected',
                               observation=observation, total_ms=round((time.perf_counter()-started)*1000, 2))
                return self.present(receipt, arguments)
            for index, step in enumerate(steps):
                action_id = request_id if not sequence else f'{request_id}:{index}'
                single = {'action_id': action_id, 'action': step['action'], 'status': 'not_executed',
                          'input_delivered': False, 'input_may_have_been_sent': False, 'expected_confirmed': False,
                          'visual_calls': 0, 'check_reuses': 0, 'relocations': 0, 'action_redos': 0}
                single['post_observation_retries'] = 0
                step_started = time.perf_counter()
                receipt['steps'].append(single)
                receipt['stopped_step'] = index
                entered_input = False
                try:
                    observation, rebound, relocations = await self.refresh_same_target(lease, context, observation, step)
                    single['relocations'] = relocations
                    bound = self.bind(rebound, observation)
                    single['observation_timing'] = {**observation.get('timing', {}), 'ocr_ms': observation.get('ocr_ms')}
                    cached = self.cache.pop(lease, None)
                    cache_id = action_fingerprint(step)
                    if cached and cached[0] == observation['observation_id'] and cached[1] == cache_id:
                        judgment, transform, timing = cached[2:]
                        timing = {**timing, 'shared_from_prior_post': True, 'remote_vision_ms': 0}
                        single['check_reuses'] = 1
                    else:
                        single['visual_calls'] += 1
                        try:
                            reply, transform, timing = await self.judge_alive(lease, context, observation, bound, stage='pre')
                        except Exception as exc:
                            if type(exc).__name__ != 'StaleObservationError' or single['relocations']:
                                raise
                            observation, rebound, count = await self.refresh_same_target(lease, context, observation, step)
                            if not count:
                                raise
                            single['relocations'] += count
                            bound = self.bind(rebound, observation)
                            single['visual_calls'] += 1
                            reply, transform, timing = await self.judge_alive(lease, context, observation, bound, stage='pre')
                        judgment = reply.current
                    single['pre_visual'] = {**judgment.model_dump(), **timing}
                    if not judgment.executable:
                        single.update(status='rejected', reason='Visual precondition not established')
                        break
                    if 'x' not in bound and (step['action'] in {'click', 'double_click', 'type'} or
                                              step['action'] == 'scroll' and step.get('target_description')):
                        if not step.get('target_description') or judgment.point is None:
                            single.update(status='rejected', reason='No reliable target location')
                            break
                        bound['x'], bound['y'] = transform.screen_point(judgment.point.x, judgment.point.y)
                        bound['_visual_fixed_coordinate'] = True
                    if 'x' in bound and judgment.point is not None:
                        predicted = transform.screen_point(judgment.point.x, judgment.point.y)
                        # For fixed targets, model must assess the requested point.
                        # A different proposed location is not authorization to move.
                        tolerance = max(2, transform.source_width/transform.sent_width)
                        if any(abs(a-b)>tolerance for a,b in zip(predicted,(bound['x'],bound['y']))):
                            single.update(status='rejected', reason='Visual point differs from requested target')
                            break
                    await self.valid(lease, context, observation, transform_box(transform))
                    bound['observation_id'] = observation['observation_id']
                    entered_input = True
                    result = await self.tool._controller_call(self.tool.controller.perform_action,
                        step['action'], bound, lease, context.task_id)
                    single.update(input_delivered=bool(result['delivered']), input_may_have_been_sent=True,
                                  screen_changed=result['screen_changed'], native_timing=result.get('timing'),
                                  target_source=result.get('target_source'))
                    post_started = time.perf_counter()
                    observation = await self.tool._enrich_observation(result['observation'],
                        self.tool._select_ocr_language_tags([], context.user_prompt))
                    self.remember(lease, observation, context.task_id)
                    next_action = steps[index+1] if index+1<len(steps) else None
                    # Only share a post/pre judgment if the next action is
                    # independently specified in the same observation. UIA IDs
                    # from the old frame cannot be silently rebound here.
                    if next_action and next_action.get('element_id'):
                        next_action = None
                    single['visual_calls'] += 1
                    try:
                        reply, transform, timing = await self.judge_alive(lease, context, observation, bound,
                            stage='post', next_action=next_action)
                    except Exception as exc:
                        if type(exc).__name__ != 'StaleObservationError':
                            raise
                        # Observe once before deciding an uncertain post-result;
                        # never repeat the click/drag/text that may have worked.
                        single['post_observation_retries'] = 1
                        single['post_refresh_change_box'] = getattr(exc, 'visual_change_box', None)
                        observation = await self.tool._observe(lease, context,
                            self.tool._select_ocr_language_tags([], context.user_prompt))
                        single['visual_calls'] += 1
                        reply, transform, timing = await self.judge_alive(lease, context, observation, bound,
                            stage='post', next_action=next_action)
                    single['post_visual'] = {**reply.current.model_dump(), **timing}
                    single['post_check_ms'] = round((time.perf_counter()-post_started)*1000, 2)
                    confirmed = (bool(step.get('expected_result')) and reply.current.safe
                                 and not reply.current.uncertainty.strip() and reply.current.expected_confirmed is True)
                    single.update(status='expected_confirmed' if confirmed else 'delivered_unconfirmed',
                                  expected_confirmed=confirmed)
                    if next_action is not None and reply.next is not None and reply.next.executable:
                        self.cache[lease] = (observation['observation_id'], action_fingerprint(next_action), reply.next, transform, timing)
                    if (step.get('expected_result') and not confirmed) or not reply.current.safe:
                        single['reason'] = 'Postcondition unconfirmed; no action replay'
                        break
                except asyncio.CancelledError:
                    single.update(status='interrupted', input_may_have_been_sent=entered_input,
                                  reason='Cancelled; do not replay without observing')
                    receipt.update(status='interrupted', input_delivered=any(s['input_delivered'] for s in receipt['steps']))
                    raise
                except Exception as exc:
                    # Do not expose arbitrary model/driver exception text.
                    no_input = not single['input_delivered'] and type(exc).__name__ in {'StaleObservationError', 'ActionRejectedError'}
                    status = ('interrupted' if isinstance(exc, InterruptedError) else
                              'outcome_unknown' if entered_input and not no_input else 'rejected')
                    single.update(status=status,
                                  input_may_have_been_sent=entered_input and not no_input, error_type=type(exc).__name__)
                    if type(exc).__name__ == 'StaleObservationError':
                        single['visual_change_box'] = getattr(exc, 'visual_change_box', None)
                    break
                finally:
                    single['total_ms'] = round((time.perf_counter()-step_started)*1000, 2)
                receipt['stopped_step'] = None
            receipt.update(status=receipt['steps'][-1]['status'],
                           input_delivered=any(s['input_delivered'] for s in receipt['steps']),
                           observation=observation, total_ms=round((time.perf_counter()-started)*1000, 2))
            receipt['visual_metrics'] = self.metrics(context.task_id)
            if not sequence:
                receipt.update({k:v for k,v in receipt['steps'][0].items() if k != 'action_id'})
                receipt['delivered'] = receipt['input_delivered']
                receipt['effect_verified'] = receipt['expected_confirmed']
            return self.present(receipt, arguments)
