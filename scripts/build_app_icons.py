"""Generate every PNG/ICO brand asset from bull.png — re-run after editing bull.png."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
BULL_SRC = STATIC / "bull.png"

NAVY = (13, 24, 41, 255)
BULL_INSET = 280 / 512   # bull width as fraction of canvas
GLOW_COLOR = (251, 191, 36)
RING_RADIUS_FRAC = 200 / 512  # ring centre radius (gap from bull bounds = 60 px)
RING_WIDTH_FRAC  = 24 / 512   # 24 px stroke at 512 — full beveled frame


def _glow_layer(bull: Image.Image, std_dev: float, opacity: float) -> Image.Image:
    alpha = bull.getchannel("A")
    blurred = alpha.filter(ImageFilter.GaussianBlur(radius=std_dev))
    halo = Image.new("RGBA", bull.size, GLOW_COLOR + (0,))
    halo.putalpha(blurred.point(lambda v: int(v * opacity)))
    return halo


def _vertical_gradient(size: int, stops: list[tuple[float, tuple[int, int, int]]]) -> Image.Image:
    """Vertical RGBA gradient sized for the full canvas — bevel mask source."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        for i in range(len(stops) - 1):
            o0, c0 = stops[i]
            o1, c1 = stops[i + 1]
            if o0 <= t <= o1:
                f = (t - o0) / (o1 - o0) if o1 > o0 else 0
                r = int(c0[0] + (c1[0] - c0[0]) * f)
                g = int(c0[1] + (c1[1] - c0[1]) * f)
                b = int(c0[2] + (c1[2] - c0[2]) * f)
                for x in range(size):
                    px[x, y] = (r, g, b, 255)
                break
    return img


def _ring_mask(size: int, r_outer: float, r_inner: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    cx = cy = size / 2
    d.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer), fill=255)
    d.ellipse((cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner), fill=0)
    return m


def build(size: int, source: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), NAVY)
    bull_size = int(round(size * BULL_INSET))
    bull = source.resize((bull_size, bull_size), Image.LANCZOS)
    cx_px = cy_px = size / 2
    bx = int(cx_px - bull_size / 2)
    by = int(cy_px - bull_size / 2)

    # Amber halo — matches navbar brand-logo intensity (α 0.45 outer / 0.75 inner).
    outer_std = 12 * size / 512
    inner_std = 6 * size / 512
    canvas.alpha_composite(_glow_layer(bull, outer_std, 0.45), (bx, by))
    canvas.alpha_composite(_glow_layer(bull, inner_std, 0.75), (bx, by))
    canvas.alpha_composite(bull, (bx, by))

    # 3D beveled gold ring — vertical gradient masked to an annulus +
    # drop-shadow filter for lift.
    ring_center_r = size * RING_RADIUS_FRAC
    ring_w        = max(2, int(round(size * RING_WIDTH_FRAC)))
    r_outer       = ring_center_r + ring_w / 2
    r_inner       = ring_center_r - ring_w / 2

    grad = _vertical_gradient(size, [
        (0.00, (0xfd, 0xe6, 0x8a)),  # bright top
        (0.35, (0xfb, 0xbf, 0x24)),  # mid amber
        (0.65, (0xd4, 0x92, 0x0c)),  # deep amber
        (1.00, (0x7c, 0x2d, 0x12)),  # burnt umber bottom
    ])
    annulus = _ring_mask(size, r_outer, r_inner)
    ring_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ring_layer = Image.composite(grad, ring_layer, annulus)

    # Drop shadow under the ring — push it off the navy face.
    shadow_alpha = annulus.filter(ImageFilter.GaussianBlur(radius=size * 0.012))
    shadow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_color = Image.new("RGBA", (size, size), (0, 0, 0, 130))
    shadow_layer = Image.composite(shadow_color, shadow_layer, shadow_alpha)
    offset = int(round(size * (4 / 512)))
    shifted = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shifted.paste(shadow_layer, (0, offset), shadow_layer)
    canvas.alpha_composite(shifted)
    canvas.alpha_composite(ring_layer)

    # Inner highlight — 1 px line just inside the bevel.
    hl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    r_hl = r_inner - 1
    hd.ellipse((cx_px - r_hl, cy_px - r_hl, cx_px + r_hl, cy_px + r_hl),
               outline=(255, 236, 160, 140), width=max(1, size // 512))
    canvas.alpha_composite(hl)
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
    fav_master.save(fav_ico, format="ICO",
                    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {fav_ico} ({fav_ico.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
