"""Coordinate transforms used only by live_computer_use (physical pixels)."""
from dataclasses import dataclass
import math


def observation_crop(observation, action, next_action=None):
    """Crop only when every supplied pointer target is in the active window.

    Description-only/global targets keep the full desktop; never assume that a
    requested target must be inside the foreground app. Returned box uses raw
    screenshot pixels, not physical screen pixels.
    """
    rect = observation.get('foreground', {}).get('rect')
    if not isinstance(rect, list) or len(rect) != 4 or any(type(v) is not int for v in rect):
        return None
    points = []
    for step in (action, next_action):
        if step is None:
            continue
        if 'x' in step and 'y' in step:
            points.append((step['x'], step['y']))
        elif step.get('action') == 'drag' and step.get('path'):
            points.extend((p['x'], p['y']) for p in step['path'])
        else:
            return None
    if not points or not all(rect[0] <= x < rect[2] and rect[1] <= y < rect[3] for x,y in points):
        return None
    ox, oy = observation['coordinate_origin']
    width, height = observation['size']
    box = (max(0,rect[0]-ox), max(0,rect[1]-oy), min(width,rect[2]-ox), min(height,rect[3]-oy))
    return box if box[0] < box[2] and box[1] < box[3] else None


def transform_box(transform):
    return (transform.crop_x, transform.crop_y, transform.crop_x+transform.source_width,
            transform.crop_y+transform.source_height)


@dataclass(frozen=True)
class ImageTransform:
    origin_x: int
    origin_y: int
    source_width: int
    source_height: int
    sent_width: int
    sent_height: int
    crop_x: int = 0
    crop_y: int = 0

    def __post_init__(self):
        if any(type(value) is not int for value in vars(self).values()):
            raise ValueError('Image geometry must use integer pixels')
        if min(self.source_width, self.source_height, self.sent_width, self.sent_height) <= 0:
            raise ValueError('Invalid image geometry')
        if min(self.crop_x, self.crop_y) < 0:
            raise ValueError('Crop offset cannot be negative')

    @staticmethod
    def _finite(*values):
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError('Invalid coordinates')

    def screen_point(self, x: float, y: float) -> tuple[int, int]:
        self._finite(x, y)
        if not (0 <= x < self.sent_width and 0 <= y < self.sent_height):
            raise ValueError('Model coordinate is outside the sent image')
        return (self.origin_x + self.crop_x + min(self.source_width-1, int(x*self.source_width/self.sent_width)),
                self.origin_y + self.crop_y + min(self.source_height-1, int(y*self.source_height/self.sent_height)))

    def image_point(self, x: int, y: int) -> tuple[float, float]:
        self._finite(x, y)
        px, py = x-self.origin_x-self.crop_x, y-self.origin_y-self.crop_y
        if not (0 <= px < self.source_width and 0 <= py < self.source_height):
            raise ValueError('Screen coordinate is outside the observed image')
        return px*self.sent_width/self.source_width, py*self.sent_height/self.source_height

    def model_point(self, x: int, y: int) -> list[int]:
        px, py = self.image_point(x, y)
        return [min(self.sent_width-1, round(px)), min(self.sent_height-1, round(py))]

    def model_rect(self, rect):
        """Clip a physical UIA rectangle into the sent image, or omit it."""
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            return None
        self._finite(*rect)
        left=max(rect[0],self.origin_x+self.crop_x)
        top=max(rect[1],self.origin_y+self.crop_y)
        right=min(rect[2]-1,self.origin_x+self.crop_x+self.source_width-1)
        bottom=min(rect[3]-1,self.origin_y+self.crop_y+self.source_height-1)
        if left>right or top>bottom:
            return None
        return self.model_point(left,top)+self.model_point(right,bottom)

    def window_point(self, x: int, y: int, rect: list[int]) -> tuple[int, int]:
        self._finite(x, y, *rect)
        if type(x) is not int or type(y) is not int:
            raise ValueError('Window-relative coordinates must use integer pixels')
        if len(rect) != 4 or not (0 <= x < rect[2]-rect[0] and 0 <= y < rect[3]-rect[1]):
            raise ValueError('Window-relative coordinate is outside the window')
        point = rect[0]+x, rect[1]+y
        self.image_point(*point)
        return point

    @staticmethod
    def css_point(x: float, y: float, content_origin: tuple[int, int], physical_per_css: float):
        # Only callers with measured browser content origin and scale may use
        # CSS coordinates; Windows DPI alone is not a browser zoom measurement.
        ImageTransform._finite(x, y, physical_per_css, *content_origin)
        if len(content_origin) != 2 or physical_per_css <= 0:
            raise ValueError('Invalid measured CSS transform')
        return (round(content_origin[0]+x*physical_per_css), round(content_origin[1]+y*physical_per_css))
