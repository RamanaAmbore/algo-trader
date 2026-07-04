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
% Palette — navy anchors the type; amber/cyan reserved for accents and
% number tags so headings stay readable on white. Darkened text tones
% (ambertxt, cyantxt, slate) used wherever a hue meets body copy.
\definecolor{ramboqnavy}{HTML}{1d2a44}
\definecolor{ramboqnavylight}{HTML}{2c3e5e}
\definecolor{ramboqamber}{HTML}{fbbf24}
\definecolor{ramboqambertxt}{HTML}{b45309}
\definecolor{ramboqcyan}{HTML}{22d3ee}
\definecolor{ramboqcyantxt}{HTML}{0e7490}
\definecolor{ramboqslate}{HTML}{334155}
\definecolor{ramboqmuted}{HTML}{7e97b8}
\definecolor{rowshade}{HTML}{eef4fb}
\definecolor{rulebar}{HTML}{fbbf24}

% Fonts — Helvetica Neue for headings (macOS system), Charter for body.
% Both ship with macOS so no MacTeX extras needed. Fallback: if the
% document is built on a box without these, xelatex silently uses
% Latin Modern (still legible).
\usepackage{fontspec}
\setmainfont{Charter}[Ligatures=TeX]
\setsansfont{Helvetica Neue}
\setmonofont{Menlo}[Scale=0.85]

\usepackage{titlesec}
\usepackage{xcolor}
\usepackage{tikz}
\usetikzlibrary{calc}

% Section (H1) — navy title with a full-width amber rule underneath.
% Number is rendered separately in amber ("§1") for a magazine look.
\titleformat{\section}[hang]
  {\color{ramboqnavy}\Large\sffamily\bfseries}
  {\color{ramboqambertxt}\S\thesection}
  {0.7em}
  {}
  [\vspace{2pt}{\color{rulebar}\titlerule[1.2pt]}]
\titlespacing*{\section}{0pt}{28pt}{10pt}

% Subsection (H2) — navy title, amber accent bar to the left of the
% number. Reads as a distinct visual step down from section.
\titleformat{\subsection}[hang]
  {\color{ramboqnavy}\large\sffamily\bfseries}
  {\color{ramboqambertxt}\thesubsection}
  {0.7em}
  {}
\titlespacing*{\subsection}{0pt}{18pt}{6pt}

% Subsubsection (H3) — muted cyantxt so it retains hierarchy without
% competing with H2. Numbered sans bold at normal size.
\titleformat{\subsubsection}[hang]
  {\color{ramboqcyantxt}\normalsize\sffamily\bfseries}
  {\thesubsubsection}
  {0.6em}
  {}
\titlespacing*{\subsubsection}{0pt}{14pt}{4pt}

% Table rows — subtle alternating shade for scannability.
\rowcolors{2}{white}{rowshade}

% Table cell padding.
\renewcommand{\arraystretch}{1.25}

% Callout / blockquote — left accent bar + subtle background for the
% ⚙ TECH boxes. Uses framed which BasicTeX ships stock.
\usepackage{framed}
\definecolor{callbg}{HTML}{f6f8fc}
\definecolor{callbar}{HTML}{22d3ee}
\renewenvironment{quote}
  {\def\FrameCommand{{\color{callbar}\vrule width 3pt}\hspace{10pt}\fboxsep=6pt\colorbox{callbg}}%
   \MakeFramed{\advance\hsize-\width\FrameRestore}\begin{minipage}{\linewidth}\vskip4pt\small\color{ramboqslate}}
  {\vskip4pt\end{minipage}\endMakeFramed}

% Header/footer rules.
\usepackage{fancyhdr}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\color{ramboqmuted}\small\sffamily RamboQuant \textbf{Design Guide}}
\fancyhead[R]{\color{ramboqmuted}\small\sffamily\thepage}
\fancyfoot[L]{\color{ramboqmuted}\scriptsize\sffamily ramboq.com}
\fancyfoot[R]{\color{ramboqmuted}\scriptsize\sffamily Ramana Ambore --- RamboQuant LLP}
\renewcommand{\headrulewidth}{0.6pt}
\renewcommand{\footrulewidth}{0.3pt}
\renewcommand{\headrule}{{\color{ramboqamber}\hrule width\headwidth height\headrulewidth \vskip-\headrulewidth}}
\renewcommand{\footrule}{{\color{ramboqmuted}\hrule width\headwidth height\footrulewidth \vskip-\footrulewidth}}

% Slightly larger inter-paragraph spacing so tables + code breathe.
\setlength{\parskip}{4pt plus 1pt}
"""
HEADER_FILE = Path("/tmp/ramboq_pdf_header.tex")
HEADER_FILE.write_text(HEADER)

# Title page — injected before the pandoc body. Highlights the author +
# scope + website. Uses only the palette colors defined in HEADER.
TITLEPAGE = r"""
\begin{titlepage}
\thispagestyle{empty}
\newgeometry{top=0in,bottom=0.75in,left=0in,right=0in}

% Full-width navy hero band. RamboQuant wordmark in amber inline.
\noindent
\begin{tikzpicture}[remember picture,overlay]
\fill[ramboqnavy] (current page.north west) rectangle ([yshift=-9cm]current page.north east);
\node[anchor=north west,xshift=1.1in,yshift=-2.2cm] at (current page.north west)
  {\color{white}\sffamily\fontsize{11}{13}\selectfont \textbf{RAMBOQUANT} \enspace$\bullet$\enspace \textcolor{ramboqamber}{Design Guide}};
\node[anchor=north west,xshift=1.1in,yshift=-3.8cm] at (current page.north west)
  {\color{white}\sffamily\fontsize{48}{54}\selectfont\bfseries Complete};
\node[anchor=north west,xshift=1.1in,yshift=-5.0cm] at (current page.north west)
  {\color{white}\sffamily\fontsize{48}{54}\selectfont\bfseries Design \textcolor{ramboqamber}{Guide}};
\node[anchor=north west,xshift=1.1in,yshift=-7.2cm] at (current page.north west)
  {\color{ramboqamber!85!white}\sffamily\Large A production trading platform, end-to-end};
\draw[ramboqamber,line width=1.4pt] ([xshift=1.1in,yshift=-8.2cm]current page.north west) -- ([xshift=2.7in,yshift=-8.2cm]current page.north west);
\end{tikzpicture}

\vspace*{9.6cm}

\begin{center}
\begin{minipage}{0.82\linewidth}
\setlength{\parskip}{0pt}
{\color{ramboqambertxt}\sffamily\small\bfseries AUTHOR}\\[4pt]
{\color{ramboqnavy}\LARGE\sffamily\bfseries Ramana Ambore}\\[3pt]
{\color{ramboqslate}\sffamily\large Platform engineer --- RamboQuant LLP}
\vspace{18pt}

{\color{ramboqambertxt}\sffamily\small\bfseries ABOUT}\\[6pt]
{\color{ramboqslate}\sffamily\small
Builds and maintains the RamboQuant platform end-to-end: a production application covering multi-broker order routing, real-time market data pipelines, options analytics, portfolio tracking, and operator plus investor-facing tooling.

\vspace{6pt}
Full-stack scope --- SvelteKit + Svelte~5 frontend, Litestar / Python async API, PostgreSQL with async SQLAlchemy 2.x, Kite / Dhan / Groww broker adapters, KiteTicker WebSocket with a shared-memory tick pipeline, Gemini-driven market summaries, MCP-integrated research tooling, and web-vitals-tracked deploys.
}
\vspace{22pt}

\hrule height 0.4pt
\vspace{10pt}
\noindent
\begin{tabular}{@{}p{0.48\linewidth}p{0.48\linewidth}@{}}
  {\color{ramboqambertxt}\sffamily\footnotesize\bfseries WEBSITE} & {\color{ramboqambertxt}\sffamily\footnotesize\bfseries GENERATED} \\[3pt]
  {\color{ramboqcyantxt}\sffamily\large\bfseries \href{https://ramboq.com}{ramboq.com}} & {\color{ramboqslate}\sffamily\normalsize\today}
\end{tabular}
\end{minipage}
\end{center}

\vfill
\restoregeometry
\end{titlepage}
"""
TITLEPAGE_FILE = Path("/tmp/ramboq_pdf_titlepage.tex")
TITLEPAGE_FILE.write_text(TITLEPAGE)

cmd = [
    "pandoc",
    str(md_file),
    "-o", str(pdf_file),
    "--toc",
    "--toc-depth=3",
    "--pdf-engine=xelatex",
    "--highlight-style=tango",
    "-H", str(HEADER_FILE),
    "-B", str(TITLEPAGE_FILE),
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
