"""Generate every PNG/ICO brand asset from bull.png — re-run after editing bull.png."""

from pathlib import Path
from PIL import Image, ImageFilter, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
BULL_SRC = STATIC / "bull.png"

NAVY = (13, 24, 41, 255)
BULL_INSET = 333 / 512  # bull width as fraction of canvas (matches app-icon.svg)
GLOW_COLOR = (251, 191, 36)  # #fbbf24 — same amber as the navbar bull glow
RING_RGBA = (255, 255, 255, 140)  # white @ alpha 0.55, matches SVG
# Ring radius as a fraction of canvas — sits 3 px outside the bull image
# bounds at 512 (tight wrap, not far away). Bull half-width is
# BULL_INSET/2 ≈ 0.325; add a small margin.
RING_RADIUS_FRAC = (333 / 512) / 2 + 3 / 512   # ≈ 0.331 → r=170 at 512
# Stroke width as a fraction (4 px on 512 canvas).
RING_WIDTH_FRAC  = 4 / 512


def _glow_layer(bull: Image.Image, std_dev: float, opacity: float) -> Image.Image:
    """Build one halo: take bull's alpha, blur it, tint amber at the given alpha."""
    alpha = bull.getchannel("A")
    blurred = alpha.filter(ImageFilter.GaussianBlur(radius=std_dev))
    halo = Image.new("RGBA", bull.size, GLOW_COLOR + (0,))
    # Modulate the per-pixel alpha by the desired opacity
    halo.putalpha(blurred.point(lambda v: int(v * opacity)))
    return halo


def build(size: int, source: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), NAVY)
    bull_size = int(round(size * BULL_INSET))
    bull = source.resize((bull_size, bull_size), Image.LANCZOS)

    # Position the bull centred on the canvas.
    bx = (size - bull_size) // 2
    by = (size - bull_size) // 2

    # Two halo layers — outer wide & faint, inner tight & brighter.
    # Scale Gaussian std-dev so the perceived radius is constant
    # across canvas sizes (12 / 6 px on the 512 canvas).
    outer_std = 12 * size / 512
    inner_std = 6 * size / 512
    # Glow alphas match the navbar brand-logo recipe so the icon reads
    # as "lit" the same way at any size.
    outer = _glow_layer(bull, outer_std, opacity=0.45)
    inner = _glow_layer(bull, inner_std, opacity=0.75)

    canvas.alpha_composite(outer, (bx, by))
    canvas.alpha_composite(inner, (bx, by))
    canvas.alpha_composite(bull,  (bx, by))

    # Subtle white ring around the bull silhouette (not the canvas edge).
    cx_px  = size / 2
    cy_px  = size / 2
    ring_r = size * RING_RADIUS_FRAC
    ring_w = max(1, int(round(size * RING_WIDTH_FRAC)))
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse(
        (cx_px - ring_r, cy_px - ring_r, cx_px + ring_r, cy_px + ring_r),
        outline=RING_RGBA,
        width=ring_w,
    )
    canvas.alpha_composite(overlay)

    return canvas


def main() -> None:
    source = Image.open(BULL_SRC).convert("RGBA")

    for size in (192, 512):
        img = build(size, source)
        out = STATIC / f"app-icon-{size}.png"
        img.save(out, format="PNG", optimize=True)
        print(f"wrote {out} ({out.stat().st_size:,} bytes)")
        if size == 512:
            logo = STATIC / "logo.png"
            img.save(logo, format="PNG", optimize=True)
            print(f"wrote {logo} ({logo.stat().st_size:,} bytes)")

    fav_master = build(256, source)
    fav_png = STATIC / "favicon.png"
    fav_master.save(fav_png, format="PNG", optimize=True)
    print(f"wrote {fav_png} ({fav_png.stat().st_size:,} bytes)")

    fav_ico = STATIC / "favicon.ico"
    fav_master.save(
        fav_ico,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"wrote {fav_ico} ({fav_ico.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
