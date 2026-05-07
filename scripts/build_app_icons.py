"""Generate every PNG/ICO brand asset from bull.png — re-run after editing bull.png."""

from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
BULL_SRC = STATIC / "bull.png"

NAVY = (13, 24, 41, 255)
BULL_INSET = 333 / 512  # bull width as fraction of canvas (matches app-icon.svg)


def _bull_for(size: int, source: Image.Image) -> Image.Image:
    bull_size = int(round(size * BULL_INSET))
    return source.resize((bull_size, bull_size), Image.LANCZOS)


def build(size: int, source: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), NAVY)
    bull = _bull_for(size, source)
    bw = bull.size[0]
    canvas.alpha_composite(bull, ((size - bw) // 2, (size - bw) // 2))
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
