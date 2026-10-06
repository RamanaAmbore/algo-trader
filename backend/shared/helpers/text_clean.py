"""Plain-text normalisation for log and alert messages.

Replaces typographic punctuation with ASCII so messages read cleanly in
files, consoles, and ntfy or Telegram. The currency symbol is kept.
"""

_REPLACEMENTS = {
    "—": "-",
    "–": "-",
    "→": "->",
    "←": "<-",
    "·": "|",
    "•": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "…": "...",
    " ": " ",
}


def to_plain(text: str) -> str:
    for src, dst in _REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text
