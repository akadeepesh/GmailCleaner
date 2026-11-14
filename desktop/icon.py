"""
GmailCleaner — the app icon.

The artwork lives in one file, desktop/assets/logo.png (a 1024 px square
master). The website (site/) uses the same artwork. The other icon files in
desktop/assets/ are generated from it with:

    python -m desktop.icon

which writes icon.png (256 px), icon.ico (16–256 px) and icon.icns. The tray
loads icon.png and falls back to resizing the master in memory if it's
missing. Needs Pillow (requirements-desktop.txt).
"""

from pathlib import Path

ACCENT = (91, 127, 255, 255)        # --accent in web/_styles.html

ASSETS = Path(__file__).resolve().parent / "assets"
MASTER = ASSETS / "logo.png"


def draw(size: int = 256):
    """Return a square RGBA PIL image of the icon at `size` pixels."""
    from PIL import Image

    if MASTER.is_file():
        return Image.open(MASTER).convert("RGBA").resize((size, size), Image.LANCZOS)
    # Last resort for a broken install: a plain accent-coloured tile.
    return Image.new("RGBA", (size, size), ACCENT)


def write_assets(directory: Path = ASSETS) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    full = draw(1024)
    png = directory / "icon.png"
    ico = directory / "icon.ico"
    icns = directory / "icon.icns"
    draw(256).save(png)
    full.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    full.save(icns)
    return [png, ico, icns]


if __name__ == "__main__":
    for path in write_assets():
        print(f"wrote {path}")
