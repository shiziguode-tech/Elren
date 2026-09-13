import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("intrinsic,expected", [(607, True), (542, False), (500, False)])
def test_settings_more_uses_actual_grid_track_without_oscillation(intrinsic, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    app = (Path(__file__).parents[1] / "deepdesk/static/app.js").read_text(encoding="utf-8")
    function = "function updateSettingsSectionScrollState" + app.split("function updateSettingsSectionScrollState", 1)[1].split("function alignActiveSettingsSectionTab", 1)[0]
    script = '''
const more={hidden:true,textContent:'',getBoundingClientRect:()=>({width:60}),setAttribute:()=>{}};
const tabs={get clientWidth(){return more.hidden?542:482},scrollLeft:0,
  get scrollWidth(){return Math.max(INTRINSIC,this.clientWidth)}};
const nav={hidden:false,dataset:{},querySelector:()=>tabs};
const $=s=>s==='#settingsSectionNav'?nav:more;
const uiText=(z,e)=>e;
''' .replace("INTRINSIC", str(intrinsic)) + function + '''
const states=[];
for(let i=0;i<6;i++){updateSettingsSectionScrollState();states.push(!more.hidden);}
nav.hidden=true;updateSettingsSectionScrollState();
console.log(JSON.stringify({states,hiddenNavHidesMore:more.hidden}));
'''
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == {"states": [expected] * 6, "hiddenNavHidesMore": True}
