"""The site's icons, from the masters in art/icon (2048 px, October 2026).

  icon_square_2048.png  the whole icon: the drawing on the green of the site (#436E59)
  foreground_2048.png   the drawing alone, sized for Android's adaptive icons (66 of 108 dp)
  monochrome_2048.png   its silhouette (themed icons; not used by the site)

From them, into docs/assets:
  icon-192.png, icon-512.png  "any": the whole icon with rounded corners (desktop, tab bar)
  icon-maskable-512.png       the drawing on the green, smaller: Android cuts it to its own shape,
                              and the whole drawing reaches 0.42 of the side from the center
                              where only 0.40 is safe
  apple-touch-icon.png        180 px, square and opaque (iOS rounds it itself)
  favicon-64.png              a tighter crop: the thin lines get lost at 16-32 px otherwise

python dev/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
MASTERS = ROOT / "art" / "icon"
OUT = ROOT / "docs" / "assets"
GREEN = (0x43, 0x6E, 0x59)
CORNER = 0.18  # of the side, like the previous icon
# where the drawing is in the square master (measured): a crop around it for the small favicon
DRAWING = (0.18, 0.21, 0.86, 0.78)


def rounded(image: Image.Image, size: int) -> Image.Image:
    image = image.resize((size, size), Image.LANCZOS).convert("RGBA")
    scale = 4  # drawn larger and reduced: smooth corners
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * scale - 1, size * scale - 1),
                                           radius=int(size * scale * CORNER), fill=255)
    image.putalpha(mask.resize((size, size), Image.LANCZOS))
    return image


def main() -> None:
    square = Image.open(MASTERS / "icon_square_2048.png").convert("RGB")
    for size in (192, 512):
        rounded(square, size).save(OUT / f"icon-{size}.png", optimize=True)
    square.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png", optimize=True)

    maskable = Image.new("RGBA", square.size, GREEN + (255,))
    maskable.alpha_composite(Image.open(MASTERS / "foreground_2048.png").convert("RGBA"))
    maskable.convert("RGB").resize((512, 512), Image.LANCZOS).save(OUT / "icon-maskable-512.png", optimize=True)

    left, top, right, bottom = (value * square.width for value in DRAWING)
    side = max(right - left, bottom - top) * 1.08
    cx, cy = (left + right) / 2, (top + bottom) / 2
    tight = square.crop((round(cx - side / 2), round(cy - side / 2), round(cx + side / 2), round(cy + side / 2)))
    rounded(tight, 64).save(OUT / "favicon-64.png", optimize=True)


if __name__ == "__main__":
    main()
