from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "elren-master.png"


def main() -> None:
    """Build every Windows/web icon size from the approved text-free master."""
    with Image.open(SOURCE) as source:
        master = source.convert("RGBA")
        master.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        if master.size != (1024, 1024):
            canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
            canvas.alpha_composite(master, ((1024 - master.width) // 2, (1024 - master.height) // 2))
            master = canvas

        # The approved generation contains a few stray pixels outside its intended
        # rounded app tile. Constrain only that exterior area; keep the mark intact.
        clean_tile = Image.new("RGBA", master.size, (246, 241, 232, 255))
        clean_tile.alpha_composite(master)
        master = clean_tile
        tile_mask = Image.new("L", master.size, 0)
        ImageDraw.Draw(tile_mask).rounded_rectangle((24, 24, 999, 999), radius=158, fill=255)
        master.putalpha(tile_mask)
        master.save(ROOT / "elren-app-icon.png", "PNG", optimize=True)

        master.resize((512, 512), Image.Resampling.LANCZOS).save(
            ROOT.parent / "deepdesk" / "static" / "elren-icon.png",
            "PNG",
            optimize=True,
        )
        master.resize((256, 256), Image.Resampling.LANCZOS).save(ROOT / "elren.png", "PNG", optimize=True)
        android_icon = (
            ROOT.parent
            / "mobile"
            / "ElrenMobile"
            / "app"
            / "src"
            / "main"
            / "res"
            / "drawable-nodpi"
            / "ic_elren.png"
        )
        android_icon.parent.mkdir(parents=True, exist_ok=True)
        master.resize((512, 512), Image.Resampling.LANCZOS).save(android_icon, "PNG", optimize=True)
        master.save(
            ROOT / "elren.ico",
            "ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            # System.Drawing.Icon is more reliable with DIB frames than PNG-compressed ICO frames.
            bitmap_format="bmp",
        )


if __name__ == "__main__":
    main()
