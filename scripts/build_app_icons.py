"""Generate every PNG/ICO brand asset from bull.png — re-run after editing bull.png."""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
BULL_SRC = STATIC / "bull.png"

NAVY = (15, 38, 56, 255)            # #0f2638 — navy diluted ~20% toward teal, radial edge + maskable pad
BULL_INSET = 260 / 512   # bull width as fraction of canvas
GLOW_COLOR = (245, 148, 16)         # #f59410 — more vivid orange-gold
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
    """Teal-centre + navy-edge radial face. Bright teal (#14525e) in
    the centre fades to the navbar navy (#0c1830) at the edge — gives
    the icon depth without competing with the orange-gold ring + halo.
    """
    img = Image.new("RGBA", (size, size), NAVY)  # #0f2638 edge (navy diluted ~20% toward teal)
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cx = cy = size / 2
    # Radial gradient via concentric ellipses with decreasing alpha from
    # the centre outward. 18 rings is enough for a smooth blend at 512 px.
    inner = (20, 82, 94)  # #14525e teal centre
    rings = 18
    for i in range(rings, 0, -1):
        t = i / rings
        r = size * 0.5 * t
        # Quadratic falloff so the lift concentrates in the centre.
        a = int(round((1 - t) ** 2 * 220))
        od.ellipse((cx - r, cy - r, cx + r, cy + r), fill=inner + (a,))
    return Image.alpha_composite(img, overlay)


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
        # Vibrant orange-gold ring — body at #f59410 with punchier
        # #ffd848 highlights at both rims. Saturation pushed up so the
        # gold reads vibrantly against the new teal-to-navy face.
        (0.00, (0xff, 0xd8, 0x48)),  # punchy gold highlight (top)
        (0.50, (0xf5, 0x94, 0x10)),  # vibrant orange-gold body
        (1.00, (0xff, 0xd8, 0x48)),  # punchy gold highlight (bottom)
    ])
    annulus = _ring_mask(size, r_outer, r_inner)
    ring_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ring_layer = Image.composite(grad, ring_layer, annulus)
    # Keep the ring close to full opacity — the brand now leans on the
    # vivid orange-gold ring as a primary visual; muting it to 60% as
    # before washed out the saturation.

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
               outline=(255, 232, 160, 110), width=max(1, size // 512))
    canvas.alpha_composite(hl)
    return canvas


def _maskable(size: int, source: Image.Image) -> Image.Image:
    """Maskable PWA icon — W3C spec says the OS may crop a circle of
    radius 40% of the icon's min dimension, so all visible content must
    sit inside the centre 80% safe zone. We render the full design at
    75% of the target size (slightly more conservative than the spec
    minimum) and paste it onto a teal-navy canvas; this absorbs every
    real-world adaptive-icon mask shape — Chrome's circle, Android's
    squircles, OEM rounded-squares — without clipping the ring or bull."""
    inner_size = int(round(size * 0.75))
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
