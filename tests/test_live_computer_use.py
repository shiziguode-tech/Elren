from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_visual_fakes import StubJudge
from PIL import Image, ImageDraw

import deepdesk.plugins.builtin.live_computer_use as live_module
from deepdesk.harness import recommended_tool_names
from deepdesk.models import AgentProfile, Risk
from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.live_computer_use import (
    LiveComputerUseController,
    LiveComputerUseTool,
    _Win32ControlBanner,
)


class NullBanner:
    instances: list[NullBanner] = []

    def __init__(self, text: str) -> None:
        self.text = text
        self.started = False
        self.stopped = False
        self.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeScreen:
    def __init__(self, size: tuple[int, int] = (300, 200)) -> None:
        self.image = Image.new("RGB", size, "white")

    def capture(self) -> Image.Image:
        return self.image.copy()

    def change(self, x: int = 10, y: int = 10) -> None:
        draw = ImageDraw.Draw(self.image)
        draw.rectangle((x, y, x + 30, y + 24), fill="black")


class FakeDriver:
    def __init__(self, screen: FakeScreen) -> None:
        self.screen = screen
        self.pointer = [-20, 10]
        self.clicks: list[tuple[int, int, int, str]] = []
        self.keys: list[list[str]] = []
        self.scrolls: list[tuple[str, int]] = []
        self.mouse_is_down = False

    def position(self):
        return SimpleNamespace(x=self.pointer[0], y=self.pointer[1])

    def size(self):
        return SimpleNamespace(width=200, height=150)

    def click(self, x: int, y: int, *, clicks: int, button: str) -> None:
        self.pointer[:] = [x, y]
        self.clicks.append((x, y, clicks, button))
        self.screen.change(x + 100, y + 50)

    def moveTo(self, x: int, y: int, *, duration: float) -> None:
        del duration
        self.pointer[:] = [x, y]

    def mouseDown(self, *, button: str) -> None:
        del button
        self.mouse_is_down = True

    def mouseUp(self, *, button: str) -> None:
        del button
        self.mouse_is_down = False

    def press(self, key: str) -> None:
        self.keys.append([key])
        self.screen.change(60, 20)

    def hotkey(self, *keys: str) -> None:
        self.keys.append(list(keys))
        self.screen.change(80, 20)

    def scroll(self, amount: int) -> None:
        self.scrolls.append(("vertical", amount))
        self.screen.change(100, 50)

    def hscroll(self, amount: int) -> None:
        self.scrolls.append(("horizontal", amount))
        self.screen.change(120, 50)


class FakeOCR:
    async def recognize(self, image_path: Path, language_tags=None):
        assert image_path.is_file()
        return {
            "source": "fake-ocr",
            "primary_language": "en-US",
            "text": "Save",
            "candidates": [
                {
                    "language": "en-US",
                    "lines": [
                        {"words": [{"text": "Save", "box": [10, 20, 30, 12]}]}
                    ],
                }
            ],
        }


class FakeWrapper:
    def __init__(self, rect: tuple[int, int, int, int]) -> None:
        self.rect = rect
        self.focused = False
        self.visible = True
        self.enabled = True

    def exists(self, timeout: float = 0) -> bool:
        del timeout
        return self.visible

    def is_visible(self) -> bool:
        return self.visible

    def is_enabled(self) -> bool:
        return self.enabled

    def rectangle(self):
        return SimpleNamespace(
            left=self.rect[0],
            top=self.rect[1],
            right=self.rect[2],
            bottom=self.rect[3],
        )

    def set_focus(self) -> None:
        self.focused = True


@pytest.fixture
def live_fixture(tmp_path, monkeypatch):
    NullBanner.instances.clear()
    screen = FakeScreen()
    driver = FakeDriver(screen)
    foreground = {
        "window_handle": 44,
        "window_title": "Elren QA",
        "process_id": 123,
        "rect": [-100, -50, 200, 150],
    }
    monkeypatch.setattr(live_module, "_foreground_window", lambda: dict(foreground))
    monkeypatch.setattr(live_module, "_window_dpi", lambda _handle: 144)
    monkeypatch.setattr(
        LiveComputerUseController,
        "_inspect_uia",
        staticmethod(lambda _foreground: ([], {})),
    )
    controller = LiveComputerUseController(
        tmp_path,
        banner_factory=NullBanner,
        driver_loader=lambda: driver,
        escape_pressed=lambda: False,
        screen_capture=screen.capture,
        screen_bounds=lambda: (-100, -50, 300, 200),
    )
    # Unit tests exercise the fake driver, not the user's real keyboard/mouse.
    # Keep unrelated coordinate/UIA assertions deterministic; the dedicated
    # physical-input test below overrides this guard explicitly.
    monkeypatch.setattr(
        LiveComputerUseController,
        "_physical_controls_down",
        staticmethod(lambda *, allowed_mouse_button="": []),
    )
    tool = LiveComputerUseTool(tmp_path, FakeOCR(), controller=controller, visual_judge=StubJudge())
    context = ToolContext(
        task_id="task-1",
        workspace=str(tmp_path),
        user_prompt="请真实操控我的电脑完成这个可逆测试",
    )
    yield tool, controller, context, screen, driver, foreground
    controller.emergency_stop("fixture_cleanup")


def test_tool_contract_is_independent_and_supports_virtual_desktop_coordinates(tmp_path):
    tool = LiveComputerUseTool(
        tmp_path,
        FakeOCR(),
        controller=SimpleNamespace(),
    )
    assert tool.name == "live_computer_use"
    actions = set(tool.parameters["properties"]["action"]["enum"])
    assert actions == {
        "start",
        "status",
        "observe",
        "click",
        "double_click",
        "type",
        "key",
        "scroll",
        "drag",
        "stop",
        "sequence",
    }
    assert "minimum" not in tool.parameters["properties"]["x"]
    assert "minimum" not in tool.parameters["properties"]["y"]
    drag_point = tool.parameters["properties"]["path"]["items"]["properties"]
    assert "minimum" not in drag_point["x"]
    assert "minimum" not in drag_point["y"]
    assert "observation_id" in tool.parameters["properties"]
    assert "element_id" in tool.parameters["properties"]
    assert tool.risk({"action": "observe"}) == Risk.SAFE
    assert tool.risk({"action": "start"}) == Risk.MEDIUM
    assert tool.risk({"action": "click"}) == Risk.HIGH


@pytest.mark.asyncio
async def test_start_observe_status_and_stop_are_lease_protected(live_fixture):
    tool, controller, context, _screen, _driver, _foreground = live_fixture
    started = await tool.execute({"action": "start", "language": "zh"}, context)
    assert started["active"] is True
    assert started["session_lease"]
    assert started["observation"]["observation_id"]
    assert started["observation"]["coordinate_origin"] == [-100, -50]
    assert started["observation"]["ocr"]["elements"][0]["screen_box"][:2] == [
        -90.0,
        -30.0,
    ]
    assert NullBanner.instances[-1].text == "正在控制电脑，按Esc强制退出"
    assert NullBanner.instances[-1].started is True

    status = await tool.execute(
        {"action": "status", "session_lease": started["session_lease"]},
        context,
    )
    assert status["active"] is True
    with pytest.raises(PermissionError, match="lease"):
        await tool.execute({"action": "status", "session_lease": "wrong"}, context)

    stopped = await tool.execute(
        {"action": "stop", "session_lease": started["session_lease"]},
        context,
    )
    assert stopped["stopped"] is True
    assert controller.public_status()["active"] is False
    assert NullBanner.instances[-1].stopped is True


@pytest.mark.asyncio
async def test_single_session_mutex_and_task_cleanup(live_fixture):
    tool, controller, context, _screen, _driver, _foreground = live_fixture
    started = await tool.execute({"action": "start"}, context)
    repeated = await tool.execute({"action": "start"}, context)
    assert repeated["already_active"] is True
    assert repeated["session_lease"] == started["session_lease"]
    with pytest.raises(RuntimeError, match="Another task"):
        controller.start("task-2", 600, "en")
    await tool.cleanup(context)
    assert controller.public_status()["stop_reason"] == "task_cleanup"


@pytest.mark.asyncio
async def test_negative_coordinate_click_is_bound_and_verified(live_fixture):
    tool, controller, context, _screen, driver, _foreground = live_fixture
    started = await tool.execute({"action": "start"}, context)
    observation = started["observation"]
    clicked = await tool.execute(
        {
            "action": "click",
            "session_lease": started["session_lease"],
            "observation_id": observation["observation_id"],
            "x": -50,
            "y": 0,
        },
        context,
    )
    assert driver.clicks == [(-50, 0, 1, "left")]
    assert clicked["target_source"] == "bound_screen_coordinates"
    assert clicked["effect_verified"] is False
    assert clicked["screen_changed"] is True
    assert clicked["status"] == "delivered_unconfirmed"
    assert clicked["observation"]["observation_id"] != observation["observation_id"]
    metrics = controller.public_status()["metrics"]
    assert metrics["action_delivery_success_rate"] == 1.0
    assert metrics["average_action_ms"] is not None


@pytest.mark.asyncio
async def test_mouse_interference_rejects_before_click_and_keeps_session(live_fixture):
    tool, controller, context, _screen, driver, _foreground = live_fixture
    started = await tool.execute({"action": "start"}, context)
    driver.pointer[:] = [20, 20]
    result = await tool.execute(
            {
                "action": "click",
                "session_lease": started["session_lease"],
                "observation_id": started["observation"]["observation_id"],
                "x": 40,
                "y": 40,
            },
            context,
        )
    assert result['status'] == 'interrupted'
    assert driver.clicks == []
    status = controller.public_status()
    assert status["active"] is True
    assert tool.visual_flow.metrics(context.task_id)['interrupted'] == 1


@pytest.mark.asyncio
async def test_physical_mouse_hold_rejects_before_input(live_fixture, monkeypatch):
    tool, _controller, context, _screen, driver, _foreground = live_fixture
    started = await tool.execute({"action": "start"}, context)
    monkeypatch.setattr(
        LiveComputerUseController,
        "_physical_controls_down",
        staticmethod(lambda *, allowed_mouse_button="": ["left_mouse"]),
    )

    result = await tool.execute(
            {
                "action": "click",
                "session_lease": started["session_lease"],
                "observation_id": started["observation"]["observation_id"],
                "x": 40,
                "y": 40,
            },
            context,
        )

    assert result['status'] == 'rejected'
    assert driver.clicks == []


@pytest.mark.asyncio
async def test_window_switch_and_target_pixel_change_reject_stale_coordinates(live_fixture):
    tool, controller, context, screen, driver, foreground = live_fixture
    started = await tool.execute({"action": "start"}, context)
    request = {
        "action": "click",
        "session_lease": started["session_lease"],
        "observation_id": started["observation"]["observation_id"],
        "x": 40,
        "y": 40,
    }
    foreground["window_handle"] = 99
    assert (await tool.execute(request, context))['status'] == 'interrupted'
    assert driver.clicks == []

    foreground["window_handle"] = 44
    refreshed = await tool.execute(
        {"action": "observe", "session_lease": started["session_lease"]},
        context,
    )
    request["observation_id"] = refreshed["observation_id"]
    screen.change(130, 75)
    assert (await tool.execute(request, context))['status'] == 'rejected'
    assert driver.clicks == []
    assert controller.public_status()["active"] is True


@pytest.mark.asyncio
async def test_explicit_coordinates_are_preserved_and_uia_id_remains_supported(live_fixture, monkeypatch):
    tool, _controller, context, _screen, driver, _foreground = live_fixture
    wrapper = FakeWrapper((-60, -20, 20, 20))
    element = {
        "element_id": "uia-save",
        "name": "Save",
        "control_type": "Button",
        "automation_id": "save",
        "rect": [-60, -20, 20, 20],
        "enabled": True,
        "suggested_action": "click",
    }
    monkeypatch.setattr(
        LiveComputerUseController,
        "_inspect_uia",
        staticmethod(
            lambda _foreground: (
                [element],
                {"uia-save": {"wrapper": wrapper, "element": element}},
            )
        ),
    )
    started = await tool.execute({"action": "start"}, context)
    common = {
        "action": "click",
        "session_lease": started["session_lease"],
        "observation_id": started["observation"]["observation_id"],
    }
    coordinate = await tool.execute({**common, "x": -30, "y": 0}, context)
    assert driver.clicks[-1][:2] == (-30, 0)
    common['observation_id'] = coordinate['observation']['observation_id']
    result = await tool.execute({**common, "element_id": "uia-save"}, context)
    assert result["target_source"] == "uia_accessibility_element"
    assert driver.clicks[-1][:2] == (-20, 0)


@pytest.mark.asyncio
async def test_missing_and_old_observation_ids_never_send_input(live_fixture):
    tool, _controller, context, _screen, driver, _foreground = live_fixture
    started = await tool.execute({"action": "start"}, context)
    for observation_id in ("", "old-id"):
        result = await tool.execute(
                {
                    "action": "click",
                    "session_lease": started["session_lease"],
                    "observation_id": observation_id,
                    "x": 30,
                    "y": 30,
                },
                context,
            )
        assert result['status'] == 'rejected'
    assert driver.clicks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["none", "pixel", "window", "moved", "disabled", "too_old", "coordinates"])
async def test_delayed_uia_requires_exact_fresh_revalidation(live_fixture, monkeypatch, mutation):
    tool, controller, context, screen, driver, foreground = live_fixture
    wrapper = FakeWrapper((-60, -20, 20, 20))
    element = {"element_id": "uia-save", "name": "Save", "control_type": "Button",
               "rect": [-60, -20, 20, 20], "enabled": True}
    monkeypatch.setattr(controller, "_inspect_uia", lambda _: (
        [element], {"uia-save": {"wrapper": wrapper, "element": element}}))
    started = await tool.execute({"action": "start"}, context)
    controller._session.observation.captured_monotonic -= 181 if mutation == "too_old" else 90
    request = {"action": "click", "session_lease": started["session_lease"],
               "observation_id": started["observation"]["observation_id"], "element_id": "uia-save"}
    if mutation == "pixel":
        screen.image.putpixel((299, 199), (0, 0, 0))
    elif mutation == "window":
        foreground["window_title"] = "Different document"
    elif mutation == "moved":
        wrapper.rect = (-59, -20, 20, 20)
    elif mutation == "disabled":
        wrapper.enabled = False
    elif mutation == "coordinates":
        request.pop("element_id")
        request.update(x=-20, y=0)
    if mutation == "none":
        result = await tool.execute(request, context)
        assert result["target_source"] == "uia_accessibility_element"
        assert len(driver.clicks) == 1
    else:
        assert (await tool.execute(request, context))['status'] in {'rejected', 'interrupted'}
        assert driver.clicks == []


@pytest.mark.asyncio
async def test_shift_delete_keeps_existing_recoverable_delete_boundary(live_fixture):
    tool, controller, context, _screen, driver, _foreground = live_fixture
    started = await tool.execute({"action": "start"}, context)
    with pytest.raises(PermissionError, match="irreversible"):
        await tool.execute(
            {
                "action": "key",
                "session_lease": started["session_lease"],
                "observation_id": started["observation"]["observation_id"],
                "keys": ["shift", "delete"],
            },
            context,
        )
    assert driver.keys == []
    assert controller.public_status()["active"] is True


def test_expiry_and_global_escape_stop_without_a_model_turn(tmp_path, monkeypatch):
    screen = FakeScreen((100, 100))
    driver = FakeDriver(screen)
    driver.pointer[:] = [10, 10]
    monkeypatch.setattr(
        live_module,
        "_foreground_window",
        lambda: {
            "window_handle": 1,
            "window_title": "QA",
            "process_id": 1,
            "rect": [0, 0, 100, 100],
        },
    )
    monkeypatch.setattr(live_module, "_window_dpi", lambda _handle: 96)
    escape = {"down": False}
    controller = LiveComputerUseController(
        tmp_path,
        banner_factory=NullBanner,
        driver_loader=lambda: driver,
        escape_pressed=lambda: escape["down"],
        screen_capture=screen.capture,
        screen_bounds=lambda: (0, 0, 100, 100),
    )
    controller.start("escape-task", 600, "en")
    escape["down"] = True
    deadline = time.monotonic() + 2
    while controller.public_status()["active"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert controller.public_status()["stop_reason"] == "escape_key"


def test_real_control_is_prioritized_only_for_explicit_visible_desktop_intent():
    explicit = recommended_tool_names("请接管我的电脑并真实点击设置", AgentProfile.GENERAL)
    ordinary = recommended_tool_names("修复网页 CSS 并截图", AgentProfile.CODER)
    assert "live_computer_use" in explicit
    assert "live_computer_use" not in ordinary


@pytest.mark.skipif(live_module.sys.platform != "win32", reason="Windows-only native banner")
def test_native_win32_banner_opens_and_closes_without_tk() -> None:
    banner = _Win32ControlBanner("正在控制电脑，按Esc强制退出")
    banner.start()
    try:
        assert banner._hwnd != 0
        assert banner._thread is not None and banner._thread.is_alive()
    finally:
        banner.stop()
    assert not banner._thread.is_alive()


@pytest.mark.asyncio
async def test_start_requires_current_foreground_control_intent(live_fixture):
    tool, controller, context, _screen, _driver, _foreground = live_fixture
    context.user_prompt = "Summarize the attached document"
    with pytest.raises(PermissionError, match="current request"):
        await tool.execute({"action": "start"}, context)
    assert controller.public_status()["active"] is False

    context.agent_profile = AgentProfile.COMPUTER_USE.value
    started = await tool.execute({"action": "start"}, context)
    assert started["active"] is True


@pytest.mark.parametrize(
    ("size", "origin", "expected_tiles"),
    [
        ((3840, 1080), (-1920, 0), 2),
        ((3840, 2160), (0, 0), 2),
        ((3000, 3000), (-1200, -900), 4),
    ],
)
@pytest.mark.asyncio
async def test_large_and_negative_origin_desktops_use_coordinate_preserving_ocr_tiles(
    tmp_path,
    size,
    origin,
    expected_tiles,
):
    screenshot = tmp_path / "virtual-desktop.png"
    Image.new("RGB", size, "white").save(screenshot)

    class RecordingOCR:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[int, int], list[str]]] = []

        async def recognize(self, image_path: Path, language_tags=None):
            with Image.open(image_path) as tile:
                tile_size = tile.size
            self.calls.append((tile_size, list(language_tags or [])))
            label = f"tile-{len(self.calls)}"
            return {
                "source": "recording-ocr",
                "primary_language": "en-US",
                "text": label,
                "candidates": [
                    {
                        "language": "en-US",
                        "lines": [
                            {
                                "text": label,
                                "words": [{"text": label, "box": [10, 20, 40, 16]}],
                            }
                        ],
                    }
                ],
            }

    ocr = RecordingOCR()
    tool = LiveComputerUseTool(tmp_path, ocr, controller=SimpleNamespace())
    enriched = await tool._enrich_observation(
        {
            "screenshot": str(screenshot),
            "coordinate_origin": list(origin),
            "uia_elements": [],
        },
        ["en-US"],
    )
    assert len(ocr.calls) == expected_tiles
    assert all(width <= 2200 and height <= 2200 for (width, height), _tags in ocr.calls)
    assert all(tags == ["en-US"] for _size, tags in ocr.calls)
    assert enriched["ocr"]["ready"] is True
    assert enriched["ocr"]["tile_count"] == expected_tiles
    assert enriched["ocr"]["successful_tile_count"] == expected_tiles
    assert enriched["ocr"]["elements"][0]["screen_box"][:2] == [
        origin[0] + 10,
        origin[1] + 20,
    ]
    assert enriched["serialized_observation_bytes"] < 80_000
    assert list(tmp_path.glob(".elren-ocr-tile-*.png")) == []


@pytest.mark.asyncio
async def test_stop_during_ocr_never_returns_a_stale_active_observation(live_fixture):
    tool, controller, context, _screen, _driver, _foreground = live_fixture
    started = await tool.execute({"action": "start"}, context)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingOCR(FakeOCR):
        async def recognize(self, image_path: Path, language_tags=None):
            entered.set()
            await release.wait()
            return await super().recognize(image_path, language_tags)

    tool.windows_ocr = BlockingOCR()
    observing = asyncio.create_task(
        tool.execute(
            {"action": "observe", "session_lease": started["session_lease"]},
            context,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert await asyncio.to_thread(controller.emergency_stop, "qa_stop_during_ocr")
    release.set()
    with pytest.raises((RuntimeError, InterruptedError, PermissionError)):
        await observing
    assert controller.public_status()["active"] is False


@pytest.mark.asyncio
async def test_emergency_stop_does_not_return_before_inflight_input_quiesces(
    live_fixture,
):
    tool, controller, context, screen, driver, _foreground = live_fixture
    entered = threading.Event()
    release = threading.Event()
    original_click = driver.click

    def blocking_click(x: int, y: int, *, clicks: int, button: str) -> None:
        entered.set()
        assert release.wait(3)
        original_click(x, y, clicks=clicks, button=button)

    driver.click = blocking_click
    started = await tool.execute({"action": "start"}, context)
    action = asyncio.create_task(
        tool.execute(
            {
                "action": "click",
                "session_lease": started["session_lease"],
                "observation_id": started["observation"]["observation_id"],
                "x": -50,
                "y": 0,
            },
            context,
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)
    stopping = asyncio.create_task(
        asyncio.to_thread(controller.emergency_stop, "qa_concurrent_stop")
    )
    await asyncio.sleep(0.08)
    assert stopping.done() is False
    release.set()
    assert (await action)['status'] == 'interrupted'
    await stopping
    click_count_at_stop_return = len(driver.clicks)
    await asyncio.sleep(0.05)
    assert len(driver.clicks) == click_count_at_stop_return == 1
    assert controller.public_status()["active"] is False
    assert controller.public_status()["metrics"]["action_attempts"] == 1
    del screen


def test_scroll_pixel_intent_preserves_both_axes_without_pyautogui_axis_aliasing():
    calls: list[tuple[int, int]] = []

    class ScrollDriver:
        @staticmethod
        def elren_scroll_pixels(scroll_x: int, scroll_y: int) -> None:
            calls.append((scroll_x, scroll_y))

    LiveComputerUseController._send_scroll_pixels(ScrollDriver(), 640, -480)
    assert calls == [(640, -480)]


def test_default_ocr_language_selection_is_bounded_and_explicit_tags_are_preserved():
    assert LiveComputerUseTool._select_ocr_language_tags([], "请点击保存") == [
        "zh-Hans",
        "en-US",
    ]
    assert LiveComputerUseTool._select_ocr_language_tags([], "Click Save") == ["en-US"]
    assert LiveComputerUseTool._select_ocr_language_tags(
        ["de-DE", "de-de", "en-US"],
        "ignored",
    ) == ["de-DE", "en-US"]


def test_escape_monitor_retries_when_the_first_quiescence_deadline_is_missed(
    tmp_path,
    monkeypatch,
):
    screen = FakeScreen((100, 100))
    driver = FakeDriver(screen)
    driver.pointer[:] = [10, 10]
    monkeypatch.setattr(
        live_module,
        "_foreground_window",
        lambda: {
            "window_handle": 1,
            "window_title": "QA",
            "process_id": 1,
            "rect": [0, 0, 100, 100],
        },
    )
    monkeypatch.setattr(live_module, "_window_dpi", lambda _handle: 96)
    escape = {"down": False}
    controller = LiveComputerUseController(
        tmp_path,
        banner_factory=NullBanner,
        driver_loader=lambda: driver,
        escape_pressed=lambda: escape["down"],
        screen_capture=screen.capture,
        screen_bounds=lambda: (0, 0, 100, 100),
    )
    original_stop = controller.emergency_stop
    calls: list[str] = []

    def miss_once(reason="emergency_stop", *, expected_session_id=""):
        calls.append(reason)
        if len(calls) == 1:
            with controller._state_lock:
                session = controller._session
                assert session is not None
                session.stopping_reason = "escape_key"
                session.stop_event.set()
            return False
        return original_stop(reason, expected_session_id=expected_session_id)

    monkeypatch.setattr(controller, "emergency_stop", miss_once)
    controller.start("escape-retry", 600, "en")
    escape["down"] = True
    deadline = time.monotonic() + 2
    while controller.public_status()["active"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert controller.public_status()["active"] is False
    assert calls[:2] == ["escape_key", "escape_key"]
