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

# Custom LaTeX header — colored headings, alternating table rows, styled
# code blocks. Uses only stock BasicTeX packages (no Eisvogel deps).
HEADER = r"""
\usepackage[table]{xcolor}
\definecolor{ramboqnavy}{HTML}{1d2a44}
\definecolor{ramboqamber}{HTML}{fbbf24}
\definecolor{ramboqcyan}{HTML}{22d3ee}
\definecolor{ramboqmuted}{HTML}{7e97b8}
\definecolor{rowshade}{HTML}{eef3f9}
\usepackage{titlesec}
\titleformat{\section}{\color{ramboqnavy}\Large\sffamily\bfseries}{\thesection}{1em}{}
\titleformat{\subsection}{\color{ramboqamber}\large\sffamily\bfseries}{\thesubsection}{1em}{}
\titleformat{\subsubsection}{\color{ramboqcyan}\normalsize\sffamily\bfseries}{\thesubsubsection}{1em}{}
\rowcolors{2}{white}{rowshade}
\usepackage{fancyhdr}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\color{ramboqmuted}\small RamboQuant Design Guide}
\fancyhead[R]{\color{ramboqmuted}\small\thepage}
\renewcommand{\headrulewidth}{0.4pt}
\renewcommand{\headrule}{{\color{ramboqamber}\hrule width\headwidth height\headrulewidth \vskip-\headrulewidth}}
"""
HEADER_FILE = Path("/tmp/ramboq_pdf_header.tex")
HEADER_FILE.write_text(HEADER)

cmd = [
    "pandoc",
    str(md_file),
    "-o", str(pdf_file),
    "--toc",
    "--toc-depth=3",
    "--pdf-engine=xelatex",
    "--highlight-style=tango",
    "-H", str(HEADER_FILE),
    "-V", "documentclass=article",
    "-V", "geometry:margin=0.75in",
    "-V", "linestretch=1.15",
    "-V", "fontsize=10.5pt",
    "-V", "colorlinks=true",
    "-V", "linkcolor=[HTML]{22d3ee}",
    "-V", "urlcolor=[HTML]{22d3ee}",
    "-V", "toccolor=[HTML]{1d2a44}",
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
