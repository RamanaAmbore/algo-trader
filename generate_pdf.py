#!/usr/bin/env python3
"""Generate DESIGN_GUIDE.pdf from DESIGN_GUIDE.md using pandoc.

Design goals:
 - Full-bleed navy cover with the author's name as the hero element
 - Second page: About Guide / About Author / Version cards
 - Palette anchored on navy + amber; no cyan/teal accents anywhere
 - Section headings styled with §-tag chips and amber underline rules
 - Footer: "page N of TOTAL" via lastpage package
"""

import subprocess
import sys
from pathlib import Path


md_file = Path("DESIGN_GUIDE.md")
pdf_file = Path("DESIGN_GUIDE.pdf")

if not md_file.exists():
    print(f"Error: {md_file} not found")
    sys.exit(1)


# --- Repo metadata for the version card ---
def _git(cmd):
    try:
        return subprocess.check_output(cmd, text=True).strip()
    except Exception:
        return ""


COMMIT_SHA = _git(["git", "rev-parse", "--short", "HEAD"]) or "local"
COMMIT_COUNT = _git(["git", "rev-list", "--count", "HEAD"]) or "?"
BRANCH = _git(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "?"


# --- LaTeX header: palette, fonts, section styling, header/footer ---
HEADER = r"""
\usepackage[table]{xcolor}

% Palette — navy anchors the type; amber is the sole accent hue.
% All prior cyan/teal tones removed per operator feedback (blue-green
% clashed with the warm navy+amber pairing). H3 + links now use a
% deep-copper amber for warmth-consistent hierarchy.
\definecolor{ramboqnavy}{HTML}{1d2a44}       % primary — headings, cover
\definecolor{ramboqnavydeep}{HTML}{131c2f}   % darker navy for cover gradient
\definecolor{ramboqnavylight}{HTML}{2c3e5e}  % hover / secondary navy
\definecolor{ramboqamber}{HTML}{fbbf24}      % primary accent — chips, rules
\definecolor{ramboqambertxt}{HTML}{a86a1e}   % amber for text on white (§ numbers)
\definecolor{ramboqcopper}{HTML}{7c3f0d}     % deep amber-copper — H3 + links
\definecolor{ramboqcream}{HTML}{fef3c7}      % pale amber — cover secondary tint
\definecolor{ramboqslate}{HTML}{334155}      % body-adjacent slate
\definecolor{ramboqmuted}{HTML}{7e97b8}      % subtle metadata
\definecolor{rowshade}{HTML}{f7f3ea}         % warm off-white for table zebra
\definecolor{callbg}{HTML}{faf7f0}           % warm cream for callout background
\definecolor{callbar}{HTML}{a86a1e}          % callout left bar — copper
\definecolor{coderule}{HTML}{e6ddc9}         % code block border tint

% Fonts — Helvetica Neue for sans headings, Charter for serif body,
% Menlo for mono. All shipped with macOS.
\usepackage{fontspec}
\setmainfont{Charter}[Ligatures=TeX]
\setsansfont{Helvetica Neue}
\setmonofont{Menlo}[Scale=0.82]

\usepackage{titlesec}
\usepackage{xcolor}
\usepackage{tikz}
\usetikzlibrary{calc,fadings,shadings}
\usepackage{lastpage}
\usepackage{hyperref}

% Hyperlink colors — no more cyan. Deep copper amber for both intra + external.
\hypersetup{
  colorlinks=true,
  linkcolor=ramboqnavy,
  urlcolor=ramboqcopper,
  citecolor=ramboqcopper,
  filecolor=ramboqcopper,
  linktoc=all
}

% Section (H1) — navy title, amber §-tag, full-width amber underline.
\titleformat{\section}[hang]
  {\color{ramboqnavy}\Large\sffamily\bfseries}
  {\color{ramboqambertxt}\S\thesection}
  {0.7em}
  {}
  [\vspace{2pt}{\color{ramboqamber}\titlerule[1.4pt]}]
\titlespacing*{\section}{0pt}{30pt}{12pt}

% Subsection (H2) — navy title, amber number.
\titleformat{\subsection}[hang]
  {\color{ramboqnavy}\large\sffamily\bfseries}
  {\color{ramboqambertxt}\thesubsection}
  {0.7em}
  {}
\titlespacing*{\subsection}{0pt}{18pt}{6pt}

% Subsubsection (H3) — deep copper (warm, complements amber/navy).
\titleformat{\subsubsection}[hang]
  {\color{ramboqcopper}\normalsize\sffamily\bfseries}
  {\thesubsubsection}
  {0.6em}
  {}
\titlespacing*{\subsubsection}{0pt}{14pt}{4pt}

% Table styling — warm off-white zebra + generous row padding.
\rowcolors{2}{white}{rowshade}
\renewcommand{\arraystretch}{1.32}

% Callout / blockquote — copper left bar + cream background.
\usepackage{framed}
\renewenvironment{quote}
  {\def\FrameCommand{{\color{callbar}\vrule width 3pt}\hspace{10pt}\fboxsep=6pt\colorbox{callbg}}%
   \MakeFramed{\advance\hsize-\width\FrameRestore}\begin{minipage}{\linewidth}\vskip4pt\small\color{ramboqslate}}
  {\vskip4pt\end{minipage}\endMakeFramed}

% Header + footer — footer carries "N of TOTAL".
\usepackage{fancyhdr}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\color{ramboqmuted}\small\sffamily RamboQuant \textbf{Design Guide}}
\fancyhead[R]{\color{ramboqmuted}\small\sffamily Ramana Ambore}
\fancyfoot[L]{\color{ramboqmuted}\scriptsize\sffamily ramboq.com}
\fancyfoot[C]{\color{ramboqnavy}\small\sffamily\bfseries \thepage\ \normalfont\color{ramboqmuted}of \pageref*{LastPage}}
\fancyfoot[R]{\color{ramboqmuted}\scriptsize\sffamily RamboQuant LLP}
\renewcommand{\headrulewidth}{0.6pt}
\renewcommand{\footrulewidth}{0.3pt}
\renewcommand{\headrule}{{\color{ramboqamber}\hrule width\headwidth height\headrulewidth \vskip-\headrulewidth}}
\renewcommand{\footrule}{{\color{ramboqmuted}\hrule width\headwidth height\footrulewidth \vskip-\footrulewidth}}

% Slightly larger inter-paragraph spacing so tables + code breathe.
\setlength{\parskip}{4pt plus 1pt}
"""

HEADER_FILE = Path("/tmp/ramboq_pdf_header.tex")
HEADER_FILE.write_text(HEADER)


# --- Front matter: cover (page 1) + about page (page 2) ---
FRONT_MATTER = rf"""
% ============================================================
% PAGE 1 — full-bleed navy cover, hero name
% ============================================================
\begin{{titlepage}}
\thispagestyle{{empty}}
\newgeometry{{margin=0in}}
\begin{{tikzpicture}}[remember picture,overlay]
  % Full-page navy fill.
  \fill[ramboqnavy] (current page.north west) rectangle (current page.south east);

  % Subtle diagonal accent — amber wedge in the top-right corner.
  \fill[ramboqamber,opacity=0.14]
    (current page.north east)
    -- ([xshift=-3.5in]current page.north east)
    -- ([xshift=-1.4in,yshift=-2.6in]current page.north east)
    -- ([yshift=-2.6in]current page.north east)
    -- cycle;

  % Concentric-ring watermark bottom-right (very low opacity).
  \foreach \i in {{1,...,22}} {{
    \draw[white,opacity=0.03,line width=0.5pt]
      ([xshift=-2.2in,yshift=2.2in]current page.south east) circle (\i*3.6mm);
  }}

  % Top brand mark.
  \node[anchor=north west,xshift=1in,yshift=-0.95in] at (current page.north west) {{%
    \color{{white}}\sffamily\normalsize\bfseries RAMBOQUANT
  }};
  \node[anchor=north west,xshift=1in,yshift=-1.2in] at (current page.north west) {{%
    \color{{ramboqamber}}\sffamily\footnotesize\bfseries DESIGN GUIDE \ \color{{white}}\textbullet\ \color{{white}}\normalfont v\,{COMMIT_COUNT}
  }};

  % Hero name — huge, two-line, amber accent on surname.
  \node[anchor=west,xshift=1in] at ([yshift=0.4in]current page.center) {{%
    \color{{white}}\sffamily\fontsize{{78}}{{86}}\selectfont\bfseries Ramana
  }};
  \node[anchor=west,xshift=1in] at ([yshift=-0.8in]current page.center) {{%
    \color{{ramboqamber}}\sffamily\fontsize{{78}}{{86}}\selectfont\bfseries Ambore
  }};

  % Amber accent rule under the name.
  \draw[ramboqamber,line width=2pt]
    ([xshift=1in,yshift=-1.55in]current page.center)
    -- ([xshift=3in,yshift=-1.55in]current page.center);

  % Role line.
  \node[anchor=west,xshift=1in] at ([yshift=-1.9in]current page.center) {{%
    \color{{white}}\sffamily\Large Platform Engineer
  }};
  \node[anchor=west,xshift=1in] at ([yshift=-2.2in]current page.center) {{%
    \color{{ramboqcream}}\sffamily\large RamboQuant LLP
  }};

  % Bottom rule.
  \draw[ramboqamber,line width=0.6pt]
    ([xshift=1in,yshift=1in]current page.south west)
    -- ([xshift=-1in,yshift=1in]current page.south east);

  % Bottom row — website left, date right.
  \node[anchor=south west,xshift=1in,yshift=0.55in] at (current page.south west) {{%
    \color{{ramboqamber}}\sffamily\footnotesize\bfseries WEBSITE
  }};
  \node[anchor=south west,xshift=1in,yshift=0.25in] at (current page.south west) {{%
    \color{{white}}\sffamily\normalsize\bfseries ramboq.com
  }};
  \node[anchor=south east,xshift=-1in,yshift=0.55in] at (current page.south east) {{%
    \color{{ramboqamber}}\sffamily\footnotesize\bfseries EDITION
  }};
  \node[anchor=south east,xshift=-1in,yshift=0.25in] at (current page.south east) {{%
    \color{{white}}\sffamily\normalsize\bfseries \today
  }};
\end{{tikzpicture}}
\end{{titlepage}}
\restoregeometry

% ============================================================
% PAGE 2 — About the Guide / About the Author / Version
% ============================================================
\thispagestyle{{empty}}
\newgeometry{{top=0.8in,bottom=0.9in,left=0.9in,right=0.9in}}

\begin{{tikzpicture}}[remember picture,overlay]
  \fill[ramboqamber] ([xshift=0.9in,yshift=-0.55in]current page.north west) rectangle ([xshift=1.1in,yshift=-1.15in]current page.north west);
\end{{tikzpicture}}

{{\color{{ramboqamber}}\sffamily\footnotesize\bfseries THE DOCUMENT}}\\[3pt]
{{\color{{ramboqnavy}}\sffamily\huge\bfseries About this guide}}\\[10pt]

{{\color{{ramboqslate}}\sffamily\small
The RamboQuant \textbf{{Complete Design Guide}} is a top-to-bottom developer + operator onboarding manual for a production trading platform. Read cover-to-cover to build a full mental model of the codebase --- or jump to any of the 42 sections for focused work.

\vspace{{6pt}}
Every subsystem section names the exact files, every architectural decision states its trade-off, and \textbf{{Part IX}} at the end is a cookbook of common change recipes with exact-diff-level guidance. The goal: anyone who reads and understands this document can modify and extend the platform confidently.
}}

\vspace{{18pt}}

% Two-column split — About Author + Version.
\noindent
\begin{{minipage}}[t]{{0.58\linewidth}}
{{\color{{ramboqamber}}\sffamily\footnotesize\bfseries THE AUTHOR}}\\[3pt]
{{\color{{ramboqnavy}}\sffamily\LARGE\bfseries Ramana Ambore}}\\[3pt]
{{\color{{ramboqcopper}}\sffamily\normalsize Platform engineer --- RamboQuant LLP}}\\[10pt]

{{\color{{ramboqslate}}\sffamily\small
Builds and maintains the RamboQuant platform end-to-end: multi-broker order routing, real-time market data pipelines, options analytics, portfolio tracking, and operator plus investor-facing tooling.

\vspace{{4pt}}
Full-stack scope --- SvelteKit + Svelte~5 frontend, Litestar / Python async API, PostgreSQL with async SQLAlchemy 2.x, Kite / Dhan / Groww broker adapters, KiteTicker WebSocket with a shared-memory tick pipeline, Gemini-driven market summaries, MCP-integrated research tooling, and web-vitals-tracked deploys.
}}
\end{{minipage}}\hfill
\begin{{minipage}}[t]{{0.36\linewidth}}
% Version card — cream box with amber border.
\begin{{tikzpicture}}
  \node[
    draw=ramboqamber,
    line width=1.2pt,
    fill=callbg,
    inner sep=12pt,
    rounded corners=3pt,
    text width=0.94\linewidth,
    align=left,
  ]{{%
    {{\color{{ramboqamber}}\sffamily\footnotesize\bfseries VERSION}}\\[5pt]
    {{\color{{ramboqnavy}}\sffamily\small\bfseries Generated}}\\
    {{\color{{ramboqslate}}\sffamily\small \today}}\\[6pt]
    {{\color{{ramboqnavy}}\sffamily\small\bfseries Revision}}\\
    {{\color{{ramboqslate}}\sffamily\small v\,{COMMIT_COUNT} \ ({COMMIT_SHA})}}\\[6pt]
    {{\color{{ramboqnavy}}\sffamily\small\bfseries Branch}}\\
    {{\color{{ramboqslate}}\sffamily\small {BRANCH}}}\\[6pt]
    {{\color{{ramboqnavy}}\sffamily\small\bfseries Pages}}\\
    {{\color{{ramboqslate}}\sffamily\small \pageref*{{LastPage}} total}}\\[6pt]
    {{\color{{ramboqnavy}}\sffamily\small\bfseries Website}}\\
    {{\color{{ramboqcopper}}\sffamily\small\bfseries \href{{https://ramboq.com}}{{ramboq.com}}}}
  }};
\end{{tikzpicture}}
\end{{minipage}}

\vspace{{24pt}}

% Bottom feature strip — three highlight cards showing "what's inside".
\noindent
{{\color{{ramboqamber}}\sffamily\footnotesize\bfseries WHAT'S INSIDE}}\\[5pt]
\begin{{tikzpicture}}
  \node[
    fill=ramboqnavy,
    inner sep=10pt,
    text width=0.28\linewidth,
    rounded corners=3pt,
    align=left,
  ]{{%
    {{\color{{ramboqamber}}\sffamily\footnotesize\bfseries 42 SECTIONS}}\\[3pt]
    {{\color{{white}}\sffamily\small Architecture, order lifecycle, brokers, frontend, runtime, and operations --- each mapped to the exact files.}}
  }};
  \hspace{{6pt}}
  \node[
    fill=ramboqnavy,
    inner sep=10pt,
    text width=0.28\linewidth,
    rounded corners=3pt,
    align=left,
  ]{{%
    {{\color{{ramboqamber}}\sffamily\footnotesize\bfseries 12 RECIPES}}\\[3pt]
    {{\color{{white}}\sffamily\small Change cookbooks --- add a route, template field, background task, broker capability, notification channel.}}
  }};
  \hspace{{6pt}}
  \node[
    fill=ramboqnavy,
    inner sep=10pt,
    text width=0.28\linewidth,
    rounded corners=3pt,
    align=left,
  ]{{%
    {{\color{{ramboqamber}}\sffamily\footnotesize\bfseries FULL STACK}}\\[3pt]
    {{\color{{white}}\sffamily\small SvelteKit + Litestar + PostgreSQL + KiteTicker + MCP --- production patterns, not toy code.}}
  }};
\end{{tikzpicture}}

\restoregeometry
\newpage
"""

FRONT_MATTER_FILE = Path("/tmp/ramboq_pdf_front.tex")
FRONT_MATTER_FILE.write_text(FRONT_MATTER)


cmd = [
    "pandoc",
    str(md_file),
    "-o", str(pdf_file),
    "--toc",
    "--toc-depth=3",
    "--pdf-engine=xelatex",
    "--highlight-style=tango",
    "-H", str(HEADER_FILE),
    "-B", str(FRONT_MATTER_FILE),
    "-V", "documentclass=article",
    "-V", "geometry:margin=0.85in",
    "-V", "linestretch=1.18",
    "-V", "fontsize=10.5pt",
    "-V", "toccolor=[HTML]{1d2a44}",
]

print(f"Generating {pdf_file}...")
result = subprocess.run(cmd, capture_output=True, text=True)

if result.returncode != 0:
    print(f"Error running pandoc: {result.stderr}")
    sys.exit(1)

print(f"✓ Generated {pdf_file}")

# Compress with pikepdf if available.
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
