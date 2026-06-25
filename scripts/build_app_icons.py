"""Generate every PNG/ICO brand asset from the source SVGs via resvg.

resvg (not cairosvg) is used because the bull-glow SVG filter chain
(feGaussianBlur + feFlood + feComposite + feMerge) is silently
dropped by cairosvg's partial filter implementation — which made the
bull glow invisible. resvg-py has full SVG filter support.

Sources of truth (all under frontend/static/):
- app-icon.svg       → favicon, PWA icons, apple-touch-icon
- og-image-home.svg  → social card (1200×630), every page
- og-image-thumb.svg → social thumb (600×600)
  (og-image-card.* retired in slice AW — was the "subpages" variant
   that public pages stopped referencing once og-image-home became
   the canonical 1200×630 share image.)

Edit the relevant SVG, then re-run this script.

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


def _render_svg_text(svg_text: str, width: int, height: int) -> Image.Image:
    """Render an arbitrary SVG string at the target dimensions via
    resvg. Used by both _render_svg (for app-icon.svg) and the og-image
    renderers (which have non-square aspect ratios)."""
    png_bytes = bytes(resvg_py.svg_to_bytes(
        svg_string=svg_text,
        width=width,
        height=height,
    ))
    return Image.open(BytesIO(png_bytes)).convert("RGBA")


def _render_svg(size: int) -> Image.Image:
    """Render app-icon.svg square to a transparent-background PIL
    Image at the target size. resvg honours the bull-glow filter
    chain (cairosvg silently drops feComposite/feMerge)."""
    return _render_svg_text(_SVG_TEXT, size, size)


def _render_og(svg_name: str, width: int, height: int) -> Image.Image:
    """Render an og-image-*.svg at its native aspect ratio. Uses the
    same resvg path so the bull-glow filter inside renders correctly
    in social cards / Slack unfurls / WhatsApp link previews."""
    text = (STATIC / svg_name).read_text()
    return _render_svg_text(text, width, height)


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

    # Open Graph share images — social cards (1200×630) + square thumb
    # (600×600). Rendered through resvg so the bull-glow filter inside
    # each SVG actually shows up on the resulting PNG (cairosvg was
    # silently dropping the filter chain, leaving bare-silhouette bulls
    # in every social preview, Slack unfurl, and WhatsApp link share).
    _save(_render_og("og-image-home.svg",  1200, 630), "og-image-home.png")
    _save(_render_og("og-image-thumb.svg",  600, 600), "og-image-thumb.png")


if __name__ == "__main__":
    main()
