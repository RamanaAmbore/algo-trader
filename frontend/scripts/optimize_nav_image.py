"""One-shot: compress nav_image.png to WebP for smaller LCP.

Run once after installing Pillow:
    pip install Pillow
    python frontend/scripts/optimize_nav_image.py

Then update the two CSS references in
  frontend/src/routes/(public)/+layout.svelte
  (two occurrences of url('/nav_image.png'))
to use url('/nav_image.webp') with a PNG fallback:

    background-image: url('/nav_image.webp'), url('/nav_image.png');

or via @supports:

    background-image: url('/nav_image.png');
    @supports (background-image: url('_.webp')) {
      background-image: url('/nav_image.webp');
    }

Keep nav_image.png in static/ as a fallback for Safari < 14 / older iOS.

TODO: run this script on the server, commit nav_image.webp, update the CSS.
"""
from pathlib import Path

src = Path("frontend/static/nav_image.png")
out = Path("frontend/static/nav_image.webp")

try:
    from PIL import Image
    img = Image.open(src).convert("RGB")
    img.save(out, "WEBP", quality=70)
    print(f"Wrote {out} ({src.stat().st_size:,} → {out.stat().st_size:,} bytes)")
except ImportError:
    print("Pillow not installed. Run: pip install Pillow")
    raise
