from __future__ import annotations

import random
from pathlib import Path

import pytest

from deepdesk.plugins.base import ToolContext
from deepdesk.plugins.builtin.background_browser import BackgroundBrowserTool


@pytest.mark.asyncio
async def test_background_browser_survives_nested_geometry_drag_slider_and_scroll(tmp_path: Path):
    """A deliberately hostile page exercises the spatial primitives used by MiniWoB-like tasks."""
    root = Path(__file__).resolve().parents[1]
    tool = BackgroundBrowserTool(workspace=root, screenshot_dir=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip("packaged browser runtime is not materialized in this checkout")
    context = ToolContext(task_id="geometry-torture", workspace=str(root))
    try:
        _browser, page = await tool._session(context)
        await page.set_content(
            """
            <style>
              body { margin: 0; min-height: 2600px; font: 16px system-ui; }
              #nested { width: 360px; height: 220px; overflow: auto; scroll-behavior: smooth;
                        margin: 40px; border: 3px solid #111; position: relative; }
              #sticky { position: sticky; top: 0; height: 38px; z-index: 4; background: white; }
              .row { height: 72px; border-bottom: 1px solid #aaa; }
              #stage { position: relative; width: 720px; height: 320px; margin: 50px;
                       transform: translateX(17px); }
              #drag-source { width: 80px; height: 58px; background: #2774ff; color: white; }
              #drop-target { position: absolute; left: 430px; top: 100px; width: 160px; height: 100px;
                             transform: rotate(3deg) scale(.92); background: #3fbf80; }
              #clipper { width: 140px; height: 60px; overflow: hidden; margin: 40px; position: relative; }
              #clipped { position: absolute; top: 30px; width: 120px; height: 60px; background: orange; }
              #covered-wrap { position: relative; width: 100px; height: 60px; margin: 40px; }
              #covered { width: 100px; height: 60px; background: pink; }
              #cover { position: absolute; inset: 0; z-index: 20; background: rgba(0,0,0,.5); }
            </style>
            <div id="nested">
              <div id="sticky">Sticky header</div>
              <div class="row">Row 1</div><div class="row">Row 2</div><div class="row">Row 3</div>
              <div class="row">Row 4</div><div class="row">Row 5</div><div class="row" id="last-row">Row 6</div>
            </div>
            <label for="slider">Precision</label>
            <input id="slider" type="range" min="0" max="10" step="0.25" value="1">
            <output id="slider-output">1</output>
            <div id="stage">
              <div id="drag-source" draggable="true">source</div>
              <div id="drop-target">target</div>
            </div>
            <div id="clipper"><div id="clipped">partly clipped</div></div>
            <div id="covered-wrap"><button id="covered">covered</button><div id="cover">overlay</div></div>
            <script>
              const slider = document.querySelector('#slider');
              slider.addEventListener('input', () => document.querySelector('#slider-output').value = slider.value);
              slider.addEventListener('change', () => document.body.dataset.sliderChanged = 'yes');
              const target = document.querySelector('#drop-target');
              target.addEventListener('dragover', event => event.preventDefault());
              target.addEventListener('drop', event => {
                event.preventDefault(); target.dataset.dropped = 'yes'; target.textContent = 'dropped';
              });
            </script>
            """
        )

        inspected = await tool.execute({"action": "inspect"}, context)
        clipped = next(item for item in inspected["layout_elements"] if item["id"] == "clipped")
        covered = next(item for item in inspected["interactive_elements"] if item["selector"] == "#covered")
        slider = next(item for item in inspected["interactive_elements"] if item["selector"] == "#slider")
        assert 0.40 <= clipped["geometry"]["visible_ratio"] <= 0.60
        assert clipped["geometry"]["coordinate_space"] == "viewport-css-pixels"
        assert clipped["geometry"]["document_bbox"]["y"] > 0
        assert covered["geometry"]["center_obscured"] is True
        assert covered["geometry"]["obscured_by"] == "#cover"
        assert slider["range"] == {"min": 0.0, "max": 10.0, "step": 0.25, "value": 1.0}

        page_scroll_before = await page.evaluate("scrollY")
        scrolled = await tool.execute(
            {"action": "scroll", "selector": "#nested", "delta_y": 430}, context
        )
        observation = scrolled["action_observation"]
        assert observation["kind"] == "scroll"
        assert observation["target"] == "#nested"
        assert observation["after"]["scroll_top"] > observation["before"]["scroll_top"]
        assert observation["settled"] is True
        assert observation["moved"] is True
        assert await page.evaluate("scrollY") == page_scroll_before

        adjusted = await tool.execute(
            {"action": "set_slider", "selector": "#slider", "value": 7.63}, context
        )
        assert adjusted["action_observation"]["kind"] == "slider"
        assert adjusted["action_observation"]["actual_value"] == 7.75
        assert await page.locator("#slider-output").text_content() == "7.75"
        assert await page.locator("body").get_attribute("data-slider-changed") == "yes"

        dragged = await tool.execute(
            {
                "action": "drag",
                "selector": "#drag-source",
                "target_selector": "#drop-target",
            },
            context,
        )
        assert await page.locator("#drop-target").get_attribute("data-dropped") == "yes"
        assert dragged["action_observation"]["kind"] == "drag"
        assert dragged["action_observation"]["source"]["width"] > 0
        assert dragged["action_observation"]["target"]["width"] > 0
    finally:
        await tool.cleanup(context)


def test_agent_prompt_requires_post_action_geometry_evidence():
    from deepdesk.engine import SYSTEM_PROMPT

    prompt = SYSTEM_PROMPT.casefold()
    assert "nested scroll" in prompt
    assert "scrolling settles" in prompt
    assert "slider" in prompt and "step" in prompt
    assert "drop" in prompt and "destination" in prompt


@pytest.mark.asyncio
async def test_background_browser_prioritizes_deep_controls_and_clamps_slider_edges(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    tool = BackgroundBrowserTool(workspace=root, screenshot_dir=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip("packaged browser runtime is not materialized in this checkout")
    context = ToolContext(task_id="deep-dom-torture", workspace=str(root))
    try:
        _browser, page = await tool._session(context)
        filler = "".join(f"<div class='filler'>filler {index}</div>" for index in range(260))
        await page.set_content(
            "<style>.filler{height:3px}.horizontal{width:140px;overflow:auto}.wide{width:900px;height:10px}</style>"
            + filler
            + "<button id='critical-final-control'>Critical</button>"
            + "<input id='edge-slider' type='range' min='-5' max='5' step='0.5' value='0'>"
            + "<input id='not-a-slider' value='text'>"
            + "<div id='horizontal' class='horizontal'><div class='wide'></div></div>"
        )

        inspected = await tool.execute({"action": "inspect"}, context)
        assert any(item["selector"] == "#critical-final-control" for item in inspected["interactive_elements"])
        assert any(item["id"] == "critical-final-control" for item in inspected["layout_elements"])

        low = await tool.execute({"action": "set_slider", "selector": "#edge-slider", "value": -99}, context)
        assert low["action_observation"]["actual_value"] == -5
        high = await tool.execute({"action": "set_slider", "selector": "#edge-slider", "value": 99}, context)
        assert high["action_observation"]["actual_value"] == 5
        with pytest.raises(Exception, match="input\\[type=range\\]"):
            await tool.execute({"action": "set_slider", "selector": "#not-a-slider", "value": 1}, context)

        horizontal = await tool.execute({"action": "scroll", "selector": "#horizontal", "delta_x": 400}, context)
        state = horizontal["action_observation"]
        assert state["after"]["scroll_left"] > 0
        assert state["moved"] is True and state["settled"] is True
    finally:
        await tool.cleanup(context)


@pytest.mark.asyncio
async def test_background_browser_precise_drop_endpoint_after_transform_and_resize(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    tool = BackgroundBrowserTool(workspace=root, screenshot_dir=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip("packaged browser runtime is not materialized in this checkout")
    context = ToolContext(task_id="precise-drop-torture", workspace=str(root))
    try:
        _browser, page = await tool._session(context)
        await page.set_content(
            """
            <style>
              #source{width:90px;height:54px;background:#245cff;color:white}
              #target{position:absolute;left:420px;top:180px;width:200px;height:110px;
                      transform:rotate(-4deg) scale(.9);background:linear-gradient(90deg,#ddd 75%,#4cdb88 75%)}
              @media(max-width:600px){#target{left:120px;top:260px;width:170px}}
            </style>
            <div id="source" draggable="true">source</div><div id="target">drop only in green edge</div>
            <script>
              const target=document.querySelector('#target');
              target.addEventListener('dragover',e=>e.preventDefault());
              target.addEventListener('drop',e=>{
                e.preventDefault(); const r=target.getBoundingClientRect();
                target.dataset.result=e.clientX>r.left+r.width*.72?'accepted':'wrong-zone';
              });
            </script>
            """
        )
        resized = await tool.execute(
            {"action": "resize", "viewport_width": 580, "viewport_height": 700}, context
        )
        target = next(item for item in resized["layout_elements"] if item["id"] == "target")
        width = target["geometry"]["element_local_size"]["width"]
        dragged = await tool.execute(
            {"action": "drag", "selector": "#source", "target_selector": "#target",
             "source_x": 45, "source_y": 27, "target_x": width * .9, "target_y": 55,
             "capture": True, "full_page": False},
            context,
        )
        assert await page.locator("#target").get_attribute("data-result") == "accepted"
        assert dragged["action_observation"]["requested_destination_position"]["x"] == pytest.approx(width * .9)
        screenshot = Path(dragged["screenshot"])
        assert screenshot.is_file() and screenshot.stat().st_size > 1000
        with pytest.raises(ValueError, match="inside the destination"):
            await tool.execute(
                {"action": "drag", "selector": "#source", "target_selector": "#target",
                 "target_x": width + 50, "target_y": 10}, context
            )
    finally:
        await tool.cleanup(context)


@pytest.mark.asyncio
async def test_background_browser_deterministic_geometry_fuzz_matrix(tmp_path: Path):
    """Twelve transformed layouts guard against fixes that only work at one magic coordinate."""
    root = Path(__file__).resolve().parents[1]
    tool = BackgroundBrowserTool(workspace=root, screenshot_dir=tmp_path)
    if tool._packaged_browser_executable() is None:
        pytest.skip("packaged browser runtime is not materialized in this checkout")
    context = ToolContext(task_id="geometry-fuzz-matrix", workspace=str(root))
    randomizer = random.Random(20260816)
    try:
        _browser, page = await tool._session(context)
        for case in range(12):
            width = randomizer.randint(480, 1180)
            target_width = randomizer.randint(130, 260)
            angle = randomizer.randint(-9, 9)
            requested = randomizer.uniform(-4, 14)
            await page.set_viewport_size({"width": width, "height": 720})
            await page.set_content(
                f"""
                <style>
                  #scroll{{height:140px;width:260px;overflow:auto;scroll-behavior:smooth}}
                  #inside{{height:740px;width:900px;background:linear-gradient(#fff,#ddd)}}
                  #source{{width:70px;height:45px;background:#2468ff}}
                  #target{{position:absolute;left:{max(110, width-330)}px;top:240px;width:{target_width}px;
                    height:90px;transform:rotate({angle}deg);background:#5ad18a}}
                </style>
                <div id="scroll"><div id="inside"></div></div>
                <input id="slider" type="range" min="-2" max="12" step="0.25" value="0">
                <div id="source" draggable="true"></div><div id="target"></div>
                <script>(()=>{{
                  const t=document.querySelector('#target'),s=document.querySelector('#source');
                  s.addEventListener('dragstart',()=>document.body.dataset.dragstart='{case}');
                  t.addEventListener('dragenter',()=>document.body.dataset.dragenter='{case}');
                  t.addEventListener('dragover',e=>e.preventDefault());
                  t.addEventListener('drop',e=>{{e.preventDefault();t.dataset.case='{case}'}});
                }})()</script>
                """
            )
            await page.evaluate("scrollTo(0,0)")
            inspected = await tool.execute({"action": "inspect"}, context)
            target = next(item for item in inspected["layout_elements"] if item["id"] == "target")
            assert target["geometry"]["visible_ratio"] > 0.7
            slider = await tool.execute(
                {"action": "set_slider", "selector": "#slider", "value": requested}, context
            )
            actual = slider["action_observation"]["actual_value"]
            assert -2 <= actual <= 12
            assert abs((actual + 2) * 4 - round((actual + 2) * 4)) < 1e-9
            scroll_result = await tool.execute(
                {"action": "scroll", "selector": "#scroll", "delta_x": 300, "delta_y": 500}, context
            )
            scroll = scroll_result["action_observation"]
            assert scroll["settled"] and scroll["moved"]
            assert scroll["after"]["scroll_left"] > 0 and scroll["after"]["scroll_top"] > 0
            await tool.execute(
                {"action": "drag", "selector": "#source", "target_selector": "#target",
                 "target_x": target["geometry"]["element_local_size"]["width"] * .5,
                 "target_y": target["geometry"]["element_local_size"]["height"] / 2},
                context,
            )
            assert await page.locator("#target").get_attribute("data-case") == str(case), await page.locator("body").evaluate("el=>({...el.dataset})")
    finally:
        await tool.cleanup(context)
