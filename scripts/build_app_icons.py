"""Generate every PNG/ICO brand asset from bull.png — re-run after editing bull.png."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
BULL_SRC = STATIC / "bull.png"

NAVY = (12, 24, 48, 255)            # #0c1830 — matches the investor navbar
BULL_INSET = 260 / 512   # bull width as fraction of canvas
GLOW_COLOR = (208, 160, 64)         # #d0a040 — orange-gold, warmer than palette champagne
RING_RADIUS_FRAC = 226 / 512  # ring centre 226/256 — ~14 px navy margin to canvas edge
RING_WIDTH_FRAC  = 20 / 512   # 20 px stroke at 512 — wider bevel for visible 3D


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


def _radial_face(size: int) -> Image.Image:
    """Navy face with a subtle radial highlight — investor navbar navy
    (#0c1830) at the edge, slightly brighter (#10223e) at the centre."""
    img = Image.new("RGBA", (size, size), NAVY)            # #0c1830 edge
    # Centre-brighter ellipse — same investor navy, lifted a few stops.
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cx = cy = size / 2
    r = size * 0.30
    od.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(16, 34, 62, 77))
    img = Image.alpha_composite(img, overlay)
    return img


def build(size: int, source: Image.Image) -> Image.Image:
    canvas = _radial_face(size)
    bull_size = int(round(size * BULL_INSET))
    bull = source.resize((bull_size, bull_size), Image.LANCZOS)
    cx_px = cy_px = size / 2
    bx = int(cx_px - bull_size / 2)
    by = int(cy_px - bull_size / 2)

    # Amber halo — three-layer bloom: an extra-wide low-alpha outer
    # bloom plus the main outer/inner glow layers at higher intensity
    # than before. The bull is the focal point; the gold glow should
    # read like emitted light, not a faint halo.
    bloom_std = 22 * size / 512
    outer_std = 12 * size / 512
    inner_std = 6  * size / 512
    canvas.alpha_composite(_glow_layer(bull, bloom_std, 0.55), (bx, by))
    canvas.alpha_composite(_glow_layer(bull, outer_std, 0.95), (bx, by))
    canvas.alpha_composite(_glow_layer(bull, inner_std, 1.00), (bx, by))
    canvas.alpha_composite(bull, (bx, by))

    # 3D beveled gold ring — vertical gradient masked to an annulus +
    # drop-shadow filter for lift.
    ring_center_r = size * RING_RADIUS_FRAC
    ring_w        = max(2, int(round(size * RING_WIDTH_FRAC)))
    r_outer       = ring_center_r + ring_w / 2
    r_inner       = ring_center_r - ring_w / 2

    grad = _vertical_gradient(size, [
        # Symmetric warm orange-gold — body shifted from palette champagne
        # #c8a84b toward orange (#d0a040), rims swapped from light gold
        # #f0d878 to the slightly warmer goldGrad mid (#f0d070). Reads
        # noticeably warmer than the pure palette tokens.
        (0.00, (0xf0, 0xd0, 0x70)),  # warm light-gold highlight (top)
        (0.50, (0xd0, 0xa0, 0x40)),  # orange-gold body
        (1.00, (0xf0, 0xd0, 0x70)),  # warm light-gold highlight (bottom)
    ])
    annulus = _ring_mask(size, r_outer, r_inner)
    ring_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ring_layer = Image.composite(grad, ring_layer, annulus)
    # Mute the ring's overall presence so the bull's amber bloom stays
    # the focal point. We keep the bevel hue but drop the alpha to ~60 %
    # so the navy face shows through and the ring reads as a quiet
    # frame, not a competing colour mass.
    r, g, b, a = ring_layer.split()
    ring_layer = Image.merge("RGBA", (r, g, b, a.point(lambda v: int(v * 0.60))))

    # Drop shadow under the ring — push it off the navy face.
    shadow_alpha = annulus.filter(ImageFilter.GaussianBlur(radius=size * 0.012))
    shadow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_color = Image.new("RGBA", (size, size), (10, 10, 10, 153))  # flood-opacity 0.6
    shadow_layer = Image.composite(shadow_color, shadow_layer, shadow_alpha)
    offset = int(round(size * (4 / 512)))
    shifted = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shifted.paste(shadow_layer, (0, offset), shadow_layer)
    canvas.alpha_composite(shifted)
    canvas.alpha_composite(ring_layer)

    # Inner highlight — 1 px line just inside the bevel. Alpha tuned
    # down (140 → 90) so it reads as a quiet bevel cue, not a second
    # bright ring competing with the muted main ring.
    hl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    r_hl = r_inner - 1
    hd.ellipse((cx_px - r_hl, cy_px - r_hl, cx_px + r_hl, cy_px + r_hl),
               outline=(255, 224, 144, 90), width=max(1, size // 512))
    canvas.alpha_composite(hl)
    return canvas


def _maskable(size: int, source: Image.Image) -> Image.Image:
    """Maskable PWA icon — W3C spec says the OS may crop a circle of
    radius 40% of the icon's min dimension, so all visible content must
    sit inside the centre 80% safe zone. We render the full design at
    80% of the target size and paste it onto a navy canvas; any crop
    the OS applies stays inside the navy margin and the ring + bull
    survive intact."""
    inner_size = int(round(size * 0.80))
    inner = build(inner_size, source)
    canvas = Image.new("RGBA", (size, size), NAVY)
    offset = (size - inner_size) // 2
    canvas.alpha_composite(inner, (offset, offset))
    return canvas


def main() -> None:
    source = Image.open(BULL_SRC).convert("RGBA")
    for size in (192, 512):
        img = build(size, source)
        out = STATIC / f"app-icon-{size}.png"
        img.save(out, format="PNG", optimize=True)
        print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    # Maskable variants — navy-padded so Android Chrome's circle crop
    # never reaches the ring or bull. Apple-touch-icon stays untouched
    # because iOS renders apple-touch-icon as-is (no mask, no crop).
    for size in (192, 512):
        img = _maskable(size, source)
        out = STATIC / f"app-icon-{size}-maskable.png"
        img.save(out, format="PNG", optimize=True)
        print(f"wrote {out} ({out.stat().st_size:,} bytes)")
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
