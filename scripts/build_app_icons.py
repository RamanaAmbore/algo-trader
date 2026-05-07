"""Generate app-icon-{192,512}.png matching frontend/static/app-icon.svg.

Pure-Pillow path (no cairosvg dependency). The PNGs feed:
  - PWA install prompts (manifest.webmanifest references both sizes)
  - Apple touch icon (apple-touch-icon link in app.html)
  - Google Search Organization.logo (/app-icon-512.png)
  - WhatsApp / Twitter / FB rich previews when crawlers prefer PNG

Design mirrors app-icon.svg exactly:
  navy background → inset navy disk → vertical-gradient gold ring
  with drop shadow → bull silhouette inset (260/512 of the canvas).
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
BULL_SRC = STATIC / "bull.png"

NAVY = (13, 24, 41, 255)
NAVY_DEEP = (12, 24, 48, 255)


def _gradient_v(size: int, stops: list[tuple[float, tuple[int, int, int]]]) -> Image.Image:
    """Vertical linear gradient, top → bottom, given (offset, rgb) stops."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    pairs = [(o, c) for o, c in stops]
    for y in range(size):
        t = y / (size - 1)
        for i in range(len(pairs) - 1):
            o0, c0 = pairs[i]
            o1, c1 = pairs[i + 1]
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
    """L-mode mask: white annulus on black background."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    cx = cy = size / 2
    d.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer), fill=255)
    d.ellipse((cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner), fill=0)
    return m


def _disk_mask(size: int, r: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    cx = cy = size / 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    return m


def build(size: int) -> Image.Image:
    s = size
    cx = cy = s / 2

    # Scale ratios from the 512-canvas SVG.
    # ring_width=14 at 512 → 14/512 = 0.02734
    # ring_outer=247 (240 stroke center + 7 half-width) at 512 → 247/512 = 0.4824
    # ring_inner=233 (240 - 7) at 512 → 233/512 = 0.4551
    # bull box=260 of 512 → 260/512 = 0.5078
    r_ring_center = s * (240 / 512)
    ring_w        = s * (14 / 512)
    r_outer       = r_ring_center + ring_w / 2
    r_inner       = r_ring_center - ring_w / 2
    r_disk_in     = s * (246 / 512)  # inset disk just under the ring
    bull_size     = int(round(s * (260 / 512)))

    # ── 1. Background: full square navy + inset darker disk ────────────
    canvas = Image.new("RGBA", (s, s), NAVY)
    disk_layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(disk_layer).ellipse(
        (cx - r_disk_in, cy - r_disk_in, cx + r_disk_in, cy + r_disk_in),
        fill=NAVY_DEEP,
    )
    canvas = Image.alpha_composite(canvas, disk_layer)

    # ── 2. Recessed-face shading (radial vignette inside the ring) ─────
    shade = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    # Step from edge inward; stronger black at the rim, fades by 60%.
    steps = 40
    for i in range(steps):
        f = i / steps
        alpha = int(115 * (1 - f) ** 2)  # quadratic falloff
        rr = r_disk_in - i * (r_disk_in * 0.55 / steps)
        sd.ellipse((cx - rr, cy - rr, cx + rr, cy + rr),
                   outline=(0, 0, 0, alpha))
    canvas = Image.alpha_composite(canvas, shade)

    # ── 3. Drop shadow under the ring ──────────────────────────────────
    shadow_mask = _ring_mask(s, r_outer + 2, r_inner - 2)
    shadow_layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sl = ImageDraw.Draw(shadow_layer)
    # Paint the annulus solid black through the mask, then blur.
    blk = Image.new("RGBA", (s, s), (0, 0, 0, 130))
    shadow_layer = Image.composite(blk, shadow_layer, shadow_mask)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=s * 0.012))
    # Offset down 4/512 px to mirror feDropShadow dy=4.
    offset = int(round(s * (4 / 512)))
    shifted = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    shifted.paste(shadow_layer, (0, offset), shadow_layer)
    canvas = Image.alpha_composite(canvas, shifted)

    # ── 4. Beveled gold ring ───────────────────────────────────────────
    grad = _gradient_v(s, [
        (0.00, (0xfd, 0xe6, 0x8a)),  # bright top
        (0.35, (0xfb, 0xbf, 0x24)),  # mid amber
        (0.65, (0xd4, 0x92, 0x0c)),  # deep amber
        (1.00, (0x7c, 0x2d, 0x12)),  # burnt umber bottom
    ])
    ring_mask = _ring_mask(s, r_outer, r_inner)
    ring_layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ring_layer = Image.composite(grad.convert("RGBA"), ring_layer, ring_mask)
    canvas = Image.alpha_composite(canvas, ring_layer)

    # ── 5. Inner highlight (1px line) ──────────────────────────────────
    hl = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    r_hl = r_inner - 1
    hd.ellipse((cx - r_hl, cy - r_hl, cx + r_hl, cy + r_hl),
               outline=(255, 236, 160, 140), width=max(1, s // 512))
    canvas = Image.alpha_composite(canvas, hl)

    # ── 6. Bull silhouette, centred, sized ~51% of canvas ──────────────
    bull = Image.open(BULL_SRC).convert("RGBA")
    bull = bull.resize((bull_size, bull_size), Image.LANCZOS)
    bx = int(cx - bull_size / 2)
    by = int(cy - bull_size / 2)
    canvas.alpha_composite(bull, (bx, by))

    return canvas


def main() -> None:
    for size in (192, 512):
        out = STATIC / f"app-icon-{size}.png"
        img = build(size)
        img.save(out, format="PNG", optimize=True)
        print(f"wrote {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
