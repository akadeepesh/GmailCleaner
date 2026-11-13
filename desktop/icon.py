"""
GmailCleaner — the app icon: an envelope with a sparkle on the accent colour.

The committed files in desktop/assets/ were generated with:

    python -m desktop.icon

which writes icon.png (256 px), icon.ico (16–256 px) and icon.icns. The tray
loads icon.png and falls back to drawing the icon in memory if it's missing.
Needs Pillow (requirements-desktop.txt).
"""

from pathlib import Path

ACCENT = (91, 127, 255, 255)        # --accent in web/_styles.html
ACCENT_DARK = (58, 79, 166, 255)    # --accent-dim
WHITE = (255, 255, 255, 255)
SPARKLE = (224, 168, 74, 255)       # --yellow

ASSETS = Path(__file__).resolve().parent / "assets"


def draw(size: int = 256):
    """Return a square RGBA PIL image of the icon."""
    from PIL import Image, ImageDraw

    big = 1024  # draw large, then downscale for smooth edges
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = big / 256

    def box(x0, y0, x1, y1):
        return [x0 * s, y0 * s, x1 * s, y1 * s]

    # Rounded tile
    d.rounded_rectangle(box(8, 8, 248, 248), radius=52 * s, fill=ACCENT)
    # Envelope body
    d.rounded_rectangle(box(44, 74, 212, 190), radius=14 * s, fill=WHITE)
    # Envelope flap (a V)
    w = int(12 * s)
    d.line([(50 * s, 84 * s), (128 * s, 142 * s), (206 * s, 84 * s)], fill=ACCENT_DARK, width=w, joint="curve")
    # Sparkle (four-pointed star) at the top right
    def star(cx, cy, r, k, fill):
        d.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
                   (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)], fill=fill)

    star(198 * s, 60 * s, 46 * s, 17 * s, ACCENT)     # outline that separates it from the envelope
    star(198 * s, 60 * s, 34 * s, 9 * s, SPARKLE)

    return img.resize((size, size), Image.LANCZOS)


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
