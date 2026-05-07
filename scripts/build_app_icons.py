"""Generate app-icon-{192,512}.png + favicon.{png,ico} + logo.png from
the same source as frontend/static/app-icon.svg.

Single brand mark across every surface: solid navy square +
bull silhouette inset at 333/512 of the canvas (the original pre-ring
proportions). No ring, no bevel, no drop shadow — the icon's shape
and size IS the navy background.

Outputs (all under frontend/static/):
  - app-icon-192.png  (PWA manifest, apple-touch-icon)
  - app-icon-512.png  (PWA manifest, Google JSON-LD Organization.logo)
  - favicon.png       (browser-tab fallback for older clients)
  - favicon.ico       (multi-size 16/32/48/64/128/256 ICO bundle)
  - logo.png          (legacy /logo.png path; preserved for back-compat)
"""

from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
BULL_SRC = STATIC / "bull.png"

NAVY = (13, 24, 41, 255)


def build(size: int) -> Image.Image:
    """Navy square canvas with the bull centred at 333/512 inset."""
    s = size
    canvas = Image.new("RGBA", (s, s), NAVY)
    bull_size = int(round(s * (333 / 512)))
    bull = Image.open(BULL_SRC).convert("RGBA").resize(
        (bull_size, bull_size), Image.LANCZOS
    )
    cx = cy = s / 2
    bx = int(cx - bull_size / 2)
    by = int(cy - bull_size / 2)
    canvas.alpha_composite(bull, (bx, by))
    return canvas


def main() -> None:
    for size in (192, 512):
        out = STATIC / f"app-icon-{size}.png"
        build(size).save(out, format="PNG", optimize=True)
        print(f"wrote {out} ({out.stat().st_size:,} bytes)")

    fav_master = build(256)
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

    logo = STATIC / "logo.png"
    build(512).save(logo, format="PNG", optimize=True)
    print(f"wrote {logo} ({logo.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
