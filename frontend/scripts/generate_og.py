"""One-shot OG image generator. Run: python frontend/scripts/generate_og.py

Requires: pip install pillow
If Pillow is unavailable, the project falls back to og-image.svg which is
already committed to frontend/static/. Modern crawlers (Facebook, Twitter,
LinkedIn, WhatsApp, Slack) accept SVG for og:image since 2022.

Usage (from repo root):
    pip install pillow
    python frontend/scripts/generate_og.py
"""
from pathlib import Path

W, H = 1200, 630
NAVY      = (12, 24, 48)      # #0c1830 — public navbar
CHAMPAGNE = (200, 168, 75)    # #c8a84b — accent
CREAM     = (240, 220, 180)   # #f0dcb4 — readable on navy
BULL_PNG  = Path(__file__).parent.parent / "static" / "bull.png"
OUT       = Path(__file__).parent.parent / "static" / "og-image.png"

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow not installed. Install with: pip install pillow")
    print("Falling back to og-image.svg (already present in static/).")
    raise SystemExit(0)

img  = Image.new("RGB", (W, H), NAVY)
draw = ImageDraw.Draw(img)

# Champagne accent bars
draw.rectangle([(0, 0), (W, 6)],   fill=CHAMPAGNE)
draw.rectangle([(0, H - 6), (W, H)], fill=CHAMPAGNE)

# Bull logo
try:
    bull = Image.open(BULL_PNG).convert("RGBA")
    bull.thumbnail((200, 200), Image.LANCZOS)
    bx = (W - bull.width) // 2
    by = 80
    img.paste(bull, (bx, by), bull)
except Exception as e:
    print(f"Could not load bull.png: {e}")

# Fonts — fall back to default if system fonts unavailable
font_paths = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

def _font(size, bold=True):
    for p in font_paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()

title_font = _font(72, bold=True)
sub_font   = _font(32, bold=False)
tag_font   = _font(18, bold=False)
url_font   = _font(14, bold=False)

# Divider line
draw.rectangle([(200, 296), (1000, 298)], fill=(*CHAMPAGNE, 140))

# Title
draw.text((W // 2, 348), "RamboQuant Analytics", fill=CREAM,
          font=title_font, anchor="mm")

# Subtitle
draw.text((W // 2, 410), "Quantitative Investment for Indian Markets",
          fill=(*CREAM, 210), font=sub_font, anchor="mm")

# Divider below subtitle
draw.rectangle([(440, 444), (760, 446)], fill=(*CHAMPAGNE, 115))

# Tagline
draw.text((W // 2, 480), "INVEST · GROW · COMPOUND",
          fill=(*CHAMPAGNE, 190), font=tag_font, anchor="mm")

# URL
draw.text((W // 2, 555), "ramboq.com",
          fill=(200, 216, 240, 115), font=url_font, anchor="mm")

img.save(OUT, "PNG", optimize=True)
print(f"Wrote {OUT}  ({W}×{H})")
