"""Explicit model double for unit tests only; never used by application code."""
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
