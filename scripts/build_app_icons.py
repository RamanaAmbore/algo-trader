"""Generate every PNG/ICO brand asset from app-icon.svg via resvg.

resvg (not cairosvg) is used because the bull-glow SVG filter chain
(feGaussianBlur + feFlood + feComposite + feMerge) is silently
dropped by cairosvg's partial filter implementation — which made the
bull glow invisible. resvg-py has full SVG filter support.

The SVG (frontend/static/app-icon.svg) is the single source of truth —
all favicon / app-icon / maskable / apple-touch-icon PNGs are rendered
from it at the appropriate size. Edit the SVG to change the design,
then re-run this script.

Maskable variants are rendered at 75% inner size and pasted onto a
teal canvas so Android Chrome's adaptive-icon crop never reaches the
ring or bull (W3C maskable safe-zone = center 80% of the canvas).
"""

from pathlib import Path
from io import BytesIO

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
SOURCE_SVG = STATIC / "app-icon.svg"

# Teal face colour (matches the flat fill in app-icon.svg) — used to
# pad the maskable canvas so the OS crop stays inside the navy margin.
TEAL_FACE = (17, 72, 88, 255)  # #114858


_SVG_TEXT = SOURCE_SVG.read_text()


def _render_svg(size: int) -> Image.Image:
    """Render app-icon.svg to a transparent-background PIL Image at the
    target size. Uses resvg (not cairosvg) so the bull-glow SVG filter
    actually renders — cairosvg silently drops feComposite/feMerge
    filter chains. resvg respects the SVG's viewBox so the design
    scales cleanly to any pixel size."""
    png_bytes = bytes(resvg_py.svg_to_bytes(
        svg_string=_SVG_TEXT,
        width=size,
        height=size,
    ))
    return Image.open(BytesIO(png_bytes)).convert("RGBA")


def _maskable(size: int) -> Image.Image:
    """Maskable PWA icon — render the design at 75% size and paste it
    onto a teal canvas. Any adaptive-icon mask shape (Chrome circle,
    Samsung squircle, OEM rounded-square) crops only into the teal
    margin, leaving the ring + bull intact."""
    inner_size = int(round(size * 0.75))
    inner = _render_svg(inner_size)
    canvas = Image.new("RGBA", (size, size), TEAL_FACE)
    offset = (size - inner_size) // 2
    canvas.alpha_composite(inner, (offset, offset))
    return canvas


def _save(img: Image.Image, name: str) -> None:
    out = STATIC / name
    img.save(out, format="PNG", optimize=True)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")


def main() -> None:
    # Square PNGs at every size the manifest + favicon links reference.
    for size in (192, 512):
        _save(_render_svg(size), f"app-icon-{size}.png")

    # Maskable variants — teal-padded.
    for size in (192, 512):
        _save(_maskable(size), f"app-icon-{size}-maskable.png")

    # iOS home-screen icon — 180×180 is the spec size; rendered straight
    # from the SVG, no mask padding (iOS doesn't apply adaptive masks).
    _save(_render_svg(180), "apple-touch-icon.png")

    # Browser tab favicon — 256×256 master, plus multi-size .ico.
    fav_master = _render_svg(256)
    _save(fav_master, "favicon.png")
    fav_ico = STATIC / "favicon.ico"
    fav_master.save(
        fav_ico, format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"wrote {fav_ico} ({fav_ico.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
