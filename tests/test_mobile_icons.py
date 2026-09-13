import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "mobile/ElrenMobile/app/src/main/res"
ANDROID = "{http://schemas.android.com/apk/res/android}"


def test_mobile_colour_icon_is_the_current_desktop_artwork():
    assert (RES / "drawable-nodpi/ic_elren.png").read_bytes() == (
        ROOT / "deepdesk/static/elren-icon.png"
    ).read_bytes()


def test_delivered_apk_contains_current_icon_not_just_updated_source():
    with zipfile.ZipFile(ROOT / "Elren.apk") as archive:
        names = archive.namelist()
        icon = next(name for name in names if name.endswith("/ic_elren.png"))
        with Image.open(io.BytesIO(archive.read(icon))) as compiled, Image.open(
            RES / "drawable-nodpi/ic_elren.png"
        ) as source:
            assert compiled.size == source.size
            assert compiled.convert("RGBA").tobytes() == source.convert("RGBA").tobytes()
        assert "res/mipmap-anydpi-v21/ic_elren_launcher.xml" in names


def test_adaptive_icons_share_artwork_and_theme_uses_single_colour_mark():
    icon = ET.parse(RES / "mipmap-anydpi/ic_elren_launcher.xml").getroot()
    assert icon.tag == "adaptive-icon"
    assert icon.find("foreground").get(ANDROID + "drawable") == "@drawable/ic_elren_launcher_foreground"
    assert icon.find("background").get(ANDROID + "drawable") == "@color/elren_icon_background"
    assert icon.find("monochrome").get(ANDROID + "drawable") == "@drawable/ic_elren_notification"
    mark = ET.parse(RES / "drawable/ic_elren_notification.xml").getroot()
    assert len(mark.findall("path")) == 1
    assert mark.find("path").get(ANDROID + "strokeLineCap") == "round"
    assert mark.find("path").get(ANDROID + "strokeColor") == "#FFFFFFFF"
