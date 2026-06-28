"""Re-compress every brand asset under frontend/static/ in place.

Idempotent: re-running produces the same bytes. Safe to call from CI or
from a pre-commit hook.

What it touches:
- *.webp        → re-encode at a tighter quality + method=6 (slowest,
                  best ratio). bull.webp keeps RGBA; nav_image.webp is
                  RGB (the background overlay buries any alpha).
- *.svg         → strip XML comments + collapse whitespace between
                  tags. Hand-written SVGs ship with extensive design
                  notes — useful for editing, dead weight on the wire.
- favicon.ico   → re-slim to 16/32/48 PNG layers (rebuild_app_icons
                  also does this, but we re-apply here so a fresh
                  `git checkout` after someone hand-edits the ICO
                  still lands at the small size).
- *.png         → re-quantize to 256 colours via Fast Octree. PNGs
                  emitted by build_app_icons.py are already quantized;
                  this catches drift if someone drops a raw PNG into
                  static/ without going through the build script.

Why no PNG→WebP conversion: og-image-home.png + og-image-thumb.png are
referenced by social-scrapers (Facebook, Twitter, WhatsApp, Slack), which
do not negotiate Accept headers. They MUST stay PNG. Favicon assets the
same — `<link rel="alternate icon">` doesn't understand WebP.

Run with the icon-venv interpreter:
    .icon-venv/bin/python scripts/optimize_images.py

Or via:
    cd frontend && npm run optimize:images
"""
from __future__ import annotations

import re
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"

# Tuned per asset. bull is foreground brand art (keep crisp). nav_image
# is behind a 78%-opacity navy overlay (tolerates aggressive compression).
WEBP_QUALITY = {
    "bull.webp": 80,
    "nav_image.webp": 60,
}
WEBP_DEFAULT_QUALITY = 75


def _optimize_png(path: Path) -> tuple[int, int]:
    """Quantize an RGBA PNG to 256-colour palette + max DEFLATE.
    Returns (before, after) byte sizes."""
    before = path.stat().st_size
    img = Image.open(path)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    q = img.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
    buf = BytesIO()
    q.save(buf, format="PNG", optimize=True, compress_level=9)
    data = buf.getvalue()
    if len(data) < before:
        path.write_bytes(data)
    return before, path.stat().st_size


def _optimize_webp(path: Path) -> tuple[int, int]:
    """Re-encode WebP at per-asset quality + slowest method (6).
    Returns (before, after) byte sizes."""
    before = path.stat().st_size
    img = Image.open(path)
    # nav_image is RGB (no alpha channel survives the navy overlay anyway).
    # bull stays RGBA — its halo glow leans on alpha edges for the gold rim.
    quality = WEBP_QUALITY.get(path.name, WEBP_DEFAULT_QUALITY)
    if path.name == "nav_image.webp" and img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=6)
    data = buf.getvalue()
    if len(data) < before:
        path.write_bytes(data)
    return before, path.stat().st_size


def _optimize_svg(path: Path) -> tuple[int, int]:
    """Strip XML comments + collapse inter-tag whitespace.

    Skips attribute values + text content (the regexes only touch
    whitespace between `>` and `<`). Embedded base64 PNGs inside <image>
    href attrs are left untouched — that's the bulk of app-icon.svg.
    Returns (before, after) byte sizes."""
    before = path.stat().st_size
    text = path.read_text(encoding="utf-8")
    # 1. Strip HTML/XML comments (non-greedy, DOTALL handles multiline).
    minified = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # 2. Collapse whitespace between adjacent tags — safe because the
    #    `>` and `<` already terminate/start tags. Pure element junction.
    minified = re.sub(r">\s+<", "><", minified)
    # 3. Collapse any leading-of-line indentation into a single space.
    #    NOT a blanket removal: a newline inside a multi-line tag like
    #    `<image\n  width="260"\n  height="260"\n  href="..."/>` MUST
    #    survive as at least one space, else the attribute names fuse.
    minified = re.sub(r"\n\s*", " ", minified)
    # 4. Collapse runs of 2+ spaces to a single space (safe inside SVG
    #    attribute lists; element text content with intentional double-
    #    spaces is not used in our brand SVGs).
    minified = re.sub(r"  +", " ", minified)
    # 5. Final pass: strip the single space we introduced at element
    #    junctions in step 3 (the `> <` artifact is also safe to collapse).
    minified = re.sub(r">\s+<", "><", minified)
    data = minified.encode("utf-8")
    if len(data) < before:
        path.write_bytes(data)
    return before, path.stat().st_size


def _optimize_favicon_ico(path: Path) -> tuple[int, int]:
    """Re-emit favicon.ico with only 16/32/48 PNG layers.
    Returns (before, after) byte sizes."""
    before = path.stat().st_size
    # Source from favicon.png (the 256×256 master). If it's already
    # quantized, the ICO inherits the small palette.
    src = STATIC / "favicon.png"
    if not src.exists():
        return before, before
    master = Image.open(src).convert("RGBA")
    # Re-quantize defensively in case favicon.png was hand-replaced.
    q = master.quantize(colors=256, method=Image.Quantize.FASTOCTREE).convert("RGBA")
    buf = BytesIO()
    q.save(buf, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    data = buf.getvalue()
    if len(data) < before:
        path.write_bytes(data)
    return before, path.stat().st_size


def main() -> int:
    if not STATIC.is_dir():
        print(f"error: {STATIC} not found", file=sys.stderr)
        return 1

    total_before = 0
    total_after = 0
    rows: list[tuple[str, int, int]] = []

    for path in sorted(STATIC.iterdir()):
        if path.is_dir():
            continue
        suffix = path.suffix.lower()
        if suffix == ".png":
            before, after = _optimize_png(path)
        elif suffix == ".webp":
            before, after = _optimize_webp(path)
        elif suffix == ".svg":
            before, after = _optimize_svg(path)
        elif path.name == "favicon.ico":
            before, after = _optimize_favicon_ico(path)
        else:
            continue
        rows.append((path.name, before, after))
        total_before += before
        total_after += after

    width = max(len(n) for n, _, _ in rows) if rows else 20
    print(f"{'asset':<{width}}  {'before':>10}  {'after':>10}  delta")
    print("-" * (width + 38))
    for name, before, after in rows:
        delta_pct = 0.0 if before == 0 else 100 * (before - after) / before
        marker = "  " if after >= before else " *"
        print(f"{name:<{width}}  {before:>10,}  {after:>10,}  {delta_pct:>5.1f}%{marker}")
    total_pct = 0.0 if total_before == 0 else 100 * (total_before - total_after) / total_before
    print("-" * (width + 38))
    print(f"{'TOTAL':<{width}}  {total_before:>10,}  {total_after:>10,}  {total_pct:>5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
