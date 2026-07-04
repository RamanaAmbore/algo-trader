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
\usepackage{xurl}                 % break long URLs on any character
\usepackage{etoolbox}             % AtBeginEnvironment for tables

% Wrap long lines in verbatim / code blocks. fvextra is the pandoc-native
% way to get line-breaking inside the Highlighting environment.
\usepackage{fvextra}
\DefineVerbatimEnvironment{Highlighting}{Verbatim}{
  breaklines=true,
  breakanywhere=true,
  breaksymbolleft={},
  breakautoindent=false,
  fontsize=\small,
  commandchars=\\\{\}
}
% Same for plain verbatim (no highlighting).
\fvset{breaklines=true,breakanywhere=true,fontsize=\small}

% Tables get \footnotesize so 2-column glossary/index rows stop
% overflowing the right margin. Also raise \tabcolsep for breathing room.
\AtBeginEnvironment{longtable}{\footnotesize}
\AtBeginEnvironment{tabular}{\footnotesize}
\AtBeginEnvironment{tabularx}{\footnotesize}
\setlength{\tabcolsep}{5pt}

% Prose overflow — allow LaTeX to stretch inter-word spacing more
% aggressively before it gives up and prints past the margin. \sloppy
% relaxes the badness threshold. Together these clean up the vast
% majority of overfull-hbox warnings from long inline-code spans.
\setlength{\emergencystretch}{6em}
\sloppy

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
\fancyfoot[C]{\color{ramboqmuted}\small\sffamily page \color{ramboqnavy}\bfseries\thepage\ \normalfont\color{ramboqmuted}of \pageref*{LastPage}}
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
% PAGE 1 — full-bleed navy cover, platform hero title
% ============================================================
\begin{{titlepage}}
\thispagestyle{{empty}}
\newgeometry{{margin=0in}}
\begin{{tikzpicture}}[remember picture,overlay]
  % Full-page navy fill.
  \fill[ramboqnavy] (current page.north west) rectangle (current page.south east);

  % Diagonal amber wedge — top-right accent.
  \fill[ramboqamber,opacity=0.14]
    (current page.north east)
    -- ([xshift=-3.5in]current page.north east)
    -- ([xshift=-1.4in,yshift=-2.6in]current page.north east)
    -- ([yshift=-2.6in]current page.north east)
    -- cycle;

  % Concentric-ring watermark bottom-right.
  \foreach \i in {{1,...,22}} {{
    \draw[white,opacity=0.035,line width=0.5pt]
      ([xshift=-2.2in,yshift=2.2in]current page.south east) circle (\i*3.6mm);
  }}

  % Top brand mark.
  \node[anchor=north west,xshift=1in,yshift=-0.95in] at (current page.north west) {{%
    \color{{white}}\sffamily\normalsize\bfseries RAMBOQUANT
  }};
  \node[anchor=north west,xshift=1in,yshift=-1.2in] at (current page.north west) {{%
    \color{{ramboqamber}}\sffamily\footnotesize\bfseries DESIGN GUIDE \ \color{{white}}\textbullet\ \color{{white}}\normalfont v\,{COMMIT_COUNT}
  }};

  % Hero title — platform name.
  % Anchor from page.west (left edge) + 0.9in so the long titles never
  % run into the amber wedge on the right. Fonts sized to fit.
  % Line 1: "RamboQuant" — 60pt white.
  \node[anchor=west] at ([xshift=0.9in,yshift=0.9in]current page.west) {{%
    \color{{white}}\sffamily\fontsize{{60}}{{68}}\selectfont\bfseries RamboQuant
  }};
  % Line 2: "Algo Trading" — 34pt amber.
  \node[anchor=west] at ([xshift=0.9in,yshift=-0.05in]current page.west) {{%
    \color{{ramboqamber}}\sffamily\fontsize{{34}}{{40}}\selectfont\bfseries Algo Trading
  }};
  % Line 3: "Platform" — 34pt white.
  \node[anchor=west] at ([xshift=0.9in,yshift=-0.65in]current page.west) {{%
    \color{{white}}\sffamily\fontsize{{34}}{{40}}\selectfont\bfseries Platform
  }};

  % Amber rule.
  \draw[ramboqamber,line width=1.8pt]
    ([xshift=0.9in,yshift=-1.2in]current page.west)
    -- ([xshift=2.9in,yshift=-1.2in]current page.west);

  % Author name + role — below rule.
  \node[anchor=west] at ([xshift=0.9in,yshift=-1.55in]current page.west) {{%
    \color{{white}}\sffamily\LARGE\bfseries Ramana R Ambore, \textcolor{{ramboqamber}}{{FRM}}
  }};
  \node[anchor=west] at ([xshift=0.9in,yshift=-1.9in]current page.west) {{%
    \color{{ramboqcream}}\sffamily\large Platform Engineer --- RamboQuant LLP
  }};

  % Bottom rule.
  \draw[ramboqamber,line width=0.6pt]
    ([xshift=1in,yshift=1in]current page.south west)
    -- ([xshift=-1in,yshift=1in]current page.south east);

  % Bottom row.
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
% PAGE 2 — About the Guide / Version / About the Author /
% Experience & Recognition / Credentials
%
% Layout invariants (durable across future edits):
% - Section order top-to-bottom: About guide → Version → Author bio →
%   Experience → Credentials.
% - Every section spans the full text width (no side columns).
% - Section labels use \sffamily\footnotesize\bfseries in ramboqamber.
% - All boxes span 0.965\linewidth for consistent right-edge alignment.
% - Copper-bordered cream boxes for reference data (Version, Credentials);
%   solid navy cards for narrative highlights (Experience & Recognition).
% ============================================================
\thispagestyle{{empty}}
\newgeometry{{top=0.55in,bottom=0.55in,left=0.75in,right=0.75in}}

\begin{{tikzpicture}}[remember picture,overlay]
  \fill[ramboqamber] ([xshift=0.75in,yshift=-0.35in]current page.north west) rectangle ([xshift=0.95in,yshift=-0.85in]current page.north west);
\end{{tikzpicture}}

% -- (1) About this guide ---------------------------------------
{{\color{{ramboqamber}}\sffamily\footnotesize\bfseries THE DOCUMENT}}\\[1pt]
{{\color{{ramboqnavy}}\sffamily\Large\bfseries About this guide}}\\[4pt]

{{\color{{ramboqslate}}\sffamily\small
The RamboQuant \textbf{{Complete Design Guide}} is a top-to-bottom developer + operator onboarding manual for a production algorithmic trading platform. Read cover-to-cover to build a full mental model of the codebase, or jump to any of the 42 sections for focused work. Every subsystem names the exact files; every architectural decision states its trade-off; \textbf{{Part IX}} is a cookbook of change recipes with exact-diff-level guidance.
}}

\vspace{{7pt}}

% -- (2) Version — full-width, left-aligned, 3-column grid inside -
{{\color{{ramboqamber}}\sffamily\footnotesize\bfseries VERSION}}\\[2pt]
\noindent
\begin{{tikzpicture}}
  \node[
    draw=ramboqcopper,
    line width=0.6pt,
    fill=callbg,
    inner sep=10pt,
    text width=0.965\linewidth,
    rounded corners=3pt,
    align=left,
  ]{{%
    \begin{{tabular}}{{@{{}}p{{0.28\linewidth}}p{{0.34\linewidth}}p{{0.34\linewidth}}@{{}}}}
      {{\color{{ramboqnavy}}\sffamily\small\bfseries Generated}} &
      {{\color{{ramboqnavy}}\sffamily\small\bfseries Revision}} &
      {{\color{{ramboqnavy}}\sffamily\small\bfseries Branch \& Pages}} \\
      {{\color{{ramboqslate}}\sffamily\small \today}} &
      {{\color{{ramboqslate}}\sffamily\small v\,{COMMIT_COUNT} \ ({COMMIT_SHA})}} &
      {{\color{{ramboqslate}}\sffamily\small {BRANCH} \ \textcolor{{ramboqcopper}}{{$\bullet$}} \ \pageref*{{LastPage}} pages}} \\[8pt]
      {{\color{{ramboqnavy}}\sffamily\small\bfseries Website}} &
      {{\color{{ramboqnavy}}\sffamily\small\bfseries Profile}} &
      {{\color{{ramboqnavy}}\sffamily\small\bfseries Tech Stack}} \\
      {{\color{{ramboqcopper}}\sffamily\small\bfseries \href{{https://ramboq.com}}{{ramboq.com}}}} &
      {{\color{{ramboqcopper}}\sffamily\small\bfseries \href{{https://ramanaambore.me}}{{ramanaambore.me}}}} &
      {{\color{{ramboqslate}}\sffamily\footnotesize SvelteKit \textcolor{{ramboqcopper}}{{$\bullet$}} Litestar \textcolor{{ramboqcopper}}{{$\bullet$}} PostgreSQL \textcolor{{ramboqcopper}}{{$\bullet$}} KiteTicker \textcolor{{ramboqcopper}}{{$\bullet$}} Kite/Dhan/Groww \textcolor{{ramboqcopper}}{{$\bullet$}} MCP \textcolor{{ramboqcopper}}{{$\bullet$}} Gemini}} \\
    \end{{tabular}}
  }};
\end{{tikzpicture}}

\vspace{{7pt}}

% -- (3) Author bio — full width --------------------------------
{{\color{{ramboqamber}}\sffamily\footnotesize\bfseries THE AUTHOR}}\\[1pt]
{{\color{{ramboqnavy}}\sffamily\Large\bfseries Ramana R Ambore, \textcolor{{ramboqcopper}}{{FRM}}}}\\[1pt]
{{\color{{ramboqcopper}}\sffamily\small Principal FinTech Engineer \& Quantitative Developer}}\\[4pt]

{{\color{{ramboqslate}}\sffamily\small
Principal FinTech engineer and quantitative developer with \textbf{{30+ years}} across mainframe modernization and cloud-native financial platforms. \textbf{{FRM}} (GARP, 2022) and \textbf{{CFA Level~2}} candidate with a Master's in Computer Science. Currently \textbf{{Principal System Analyst}} at \textbf{{Fidelity Investments}}, leading billing-platform modernization on AWS + Snowflake, alongside proprietary legacy modernization tooling. Deep specialism in derivatives risk and options pricing --- Black-Scholes, Greeks modeling, multi-leg strategy analytics --- carried directly into RamboQuant's derivatives layer. \textbf{{NTT Innovation Award}} recipient (top-40 global innovator). Based in Merrimack, NH.
}}

\vspace{{7pt}}

% -- (4) Experience & Recognition — three navy cards, full width --
% Cards use \minipage side-by-side (not tikz \hspace which leaves
% invisible margin whitespace at the right edge). This lets us hit
% the full 0.965\linewidth cleanly across three ~32%-width cards.
{{\color{{ramboqamber}}\sffamily\footnotesize\bfseries EXPERIENCE \& RECOGNITION}}\\[2pt]
\noindent\hspace*{{0pt}}%
\begin{{minipage}}[t]{{0.318\linewidth}}
\begin{{tikzpicture}}
  \node[
    fill=ramboqnavy,
    inner sep=9pt,
    text width=\linewidth,
    minimum height=1.15in,
    rounded corners=3pt,
    align=left,
  ]{{%
    {{\color{{ramboqamber}}\sffamily\scriptsize\bfseries CURRENT ROLE}}\\[2pt]
    {{\color{{white}}\sffamily\small\bfseries Principal System Analyst}}\\
    {{\color{{ramboqcream}}\sffamily\footnotesize Fidelity Investments}}\\[3pt]
    {{\color{{white}}\sffamily\scriptsize Billing platform modernization on AWS + Snowflake; distributed fee-calculation engines.}}
  }};
\end{{tikzpicture}}
\end{{minipage}}\hfill
\begin{{minipage}}[t]{{0.318\linewidth}}
\begin{{tikzpicture}}
  \node[
    fill=ramboqnavy,
    inner sep=9pt,
    text width=\linewidth,
    minimum height=1.15in,
    rounded corners=3pt,
    align=left,
  ]{{%
    {{\color{{ramboqamber}}\sffamily\scriptsize\bfseries INDUSTRY DEPTH}}\\[2pt]
    {{\color{{white}}\sffamily\small\bfseries 30+ years FinTech}}\\
    {{\color{{ramboqcream}}\sffamily\footnotesize Mainframe to cloud-native}}\\[3pt]
    {{\color{{white}}\sffamily\scriptsize Derivatives risk, options pricing (Black-Scholes, Greeks), multi-leg strategy analytics.}}
  }};
\end{{tikzpicture}}
\end{{minipage}}\hfill
\begin{{minipage}}[t]{{0.318\linewidth}}
\begin{{tikzpicture}}
  \node[
    fill=ramboqnavy,
    inner sep=9pt,
    text width=\linewidth,
    minimum height=1.15in,
    rounded corners=3pt,
    align=left,
  ]{{%
    {{\color{{ramboqamber}}\sffamily\scriptsize\bfseries RECOGNITION}}\\[2pt]
    {{\color{{white}}\sffamily\small\bfseries NTT Innovation Award}}\\
    {{\color{{ramboqcream}}\sffamily\footnotesize Top-40 global innovator}}\\[3pt]
    {{\color{{white}}\sffamily\scriptsize Selected globally for innovation contributions in financial-services engineering.}}
  }};
\end{{tikzpicture}}
\end{{minipage}}

\vspace{{7pt}}

% -- (5) Credentials — full-width copper strip -------------------
{{\color{{ramboqamber}}\sffamily\footnotesize\bfseries CREDENTIALS}}\\[2pt]
\noindent
\begin{{tikzpicture}}
  \node[
    draw=ramboqcopper,
    line width=0.6pt,
    fill=callbg,
    inner sep=8pt,
    text width=0.965\linewidth,
    rounded corners=3pt,
    align=left,
  ]{{%
    {{\color{{ramboqnavy}}\sffamily\small
      \textbf{{FRM}} (GARP, 2022) \ \textcolor{{ramboqcopper}}{{$\bullet$}} \
      \textbf{{CFA Level~2}} \ \textcolor{{ramboqcopper}}{{$\bullet$}} \
      \textbf{{Master's, Computer Science}} \ \textcolor{{ramboqcopper}}{{$\bullet$}} \
      Six Sigma Green Belt \ \textcolor{{ramboqcopper}}{{$\bullet$}} \
      IBM Certified DB2 DBA \ \textcolor{{ramboqcopper}}{{$\bullet$}} \
      Sun Certified Java Programmer
    }}
  }};
\end{{tikzpicture}}

\restoregeometry
\newpage
"""

FRONT_MATTER_FILE = Path("/tmp/ramboq_pdf_front.tex")
FRONT_MATTER_FILE.write_text(FRONT_MATTER)


# Lua filter — forces explicit column widths on every table so `p{...}`
# columns wrap long cells instead of overflowing the right margin.
LUA_FILTER = r"""
function Table(tbl)
  local ncols = #tbl.colspecs
  if ncols == 2 then
    tbl.colspecs[1][2] = 0.24
    tbl.colspecs[2][2] = 0.72
  elseif ncols == 3 then
    tbl.colspecs[1][2] = 0.22
    tbl.colspecs[2][2] = 0.35
    tbl.colspecs[3][2] = 0.37
  elseif ncols == 4 then
    tbl.colspecs[1][2] = 0.20
    tbl.colspecs[2][2] = 0.25
    tbl.colspecs[3][2] = 0.25
    tbl.colspecs[4][2] = 0.26
  else
    local share = 0.96 / ncols
    for i, col in ipairs(tbl.colspecs) do
      col[2] = share
    end
  end
  return tbl
end
"""
LUA_FILTER_FILE = Path("/tmp/ramboq_pdf_tables.lua")
LUA_FILTER_FILE.write_text(LUA_FILTER)

cmd = [
    "pandoc",
    str(md_file),
    "-o", str(pdf_file),
    "--toc",
    "--toc-depth=3",
    "--pdf-engine=xelatex",
    "--highlight-style=tango",
    "--lua-filter", str(LUA_FILTER_FILE),
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
