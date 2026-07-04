#!/usr/bin/env python3
"""Generate DESIGN_GUIDE.pdf from DESIGN_GUIDE.md using pandoc."""

import subprocess
import sys
from pathlib import Path

md_file = Path("DESIGN_GUIDE.md")
pdf_file = Path("DESIGN_GUIDE.pdf")

if not md_file.exists():
    print(f"Error: {md_file} not found")
    sys.exit(1)

# Generate PDF using pandoc
cmd = [
    "pandoc",
    str(md_file),
    "-o", str(pdf_file),
    "--toc",
    "--toc-depth=2",
    "--pdf-engine=xelatex",
    "-V", "documentclass=article",
    "-V", "geometry:margin=0.75in",
    "-V", "linestretch=1.1",
]

print(f"Generating {pdf_file}...")
result = subprocess.run(cmd, capture_output=True, text=True)

if result.returncode != 0:
    print(f"Error running pandoc: {result.stderr}")
    sys.exit(1)

print(f"✓ Generated {pdf_file}")

# Try to compress with pikepdf if available
try:
    import pikepdf
    print("Compressing PDF...")
    p = pikepdf.open(str(pdf_file), allow_overwriting_input=True)
    p.save(str(pdf_file), compress_streams=True)
    print(f"✓ Compressed {pdf_file}")
except ImportError:
    print("(pikepdf not available — skipping compression)")
except Exception as e:
    print(f"Warning: Could not compress PDF: {e}")

file_size = pdf_file.stat().st_size / (1024 * 1024)
print(f"Final size: {file_size:.2f} MB")
