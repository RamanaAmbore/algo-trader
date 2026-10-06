"""Error alert helpers: readable messages and the repeat gate used by the error event agent.

Classification: an ERROR alerts only when its message repeats more than REPEAT_THRESHOLD
times in WINDOW_S (a persistent failure). Earlier repeats are not sent. A record logged with
``extra={"alert_now": True}`` (a failure with no automatic recovery) alerts on its first
occurrence. A given message alerts at most once per COOLDOWN_S, and the next alert reports
how many repeats were suppressed.
"""
import re
import threading
import time
from collections import deque

from backend.shared.helpers.text_clean import to_plain

COOLDOWN_S = 900
WINDOW_S = 900
REPEAT_THRESHOLD = 3
_MAX_MSG_CHARS = 300
_MAX_TRACKED = 1000
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HTML_RE = re.compile(r"<html.*?</html>", re.IGNORECASE | re.DOTALL)
_BYTES_PREFIX_RE = re.compile(r"\(b'")
_BYTES_SUFFIX_RE = re.compile(r"'\)\s*$")


def _html_section_text(match) -> str:
    title = _TITLE_RE.search(match.group(0))
    return title.group(1) if title else _TAG_RE.sub(" ", match.group(0))


def clean_message(msg: str) -> str:
    """Return a one-line, readable form of a log message.

    HTML error pages (for example a gateway's 502 page) collapse to their
    title. Escaped newlines and bytes wrappers are removed, whitespace is
    collapsed, and the result is capped at _MAX_MSG_CHARS.
    """
    text = to_plain(msg or "")
    text = _HTML_RE.sub(_html_section_text, text)
    text = _TAG_RE.sub(" ", text) if "<" in text and ">" in text else text
    text = text.replace("\\r", " ").replace("\\n", " ")
    text = text.replace("\r", " ").replace("\n", " ")
    text = _BYTES_PREFIX_RE.sub("", text)
    text = _BYTES_SUFFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _MAX_MSG_CHARS:
        text = text[: _MAX_MSG_CHARS - 1].rstrip() + "…"
    return text


class RepeatGate:
    """Per-message decision: should this error occurrence alert, and how many repeats to report."""

    def __init__(self, threshold: int = REPEAT_THRESHOLD, window_s: float = WINDOW_S,
                 cooldown_s: float = COOLDOWN_S, clock=time.monotonic):
        self._threshold = threshold
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._last: dict[str, float] = {}
        self._repeats: dict[str, int] = {}
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def decide(self, key: str, alert_now: bool = False) -> int | None:
        """Return the repeat count to report when this occurrence alerts, else None."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            hits.append(now)
            while hits and now - hits[0] > self._window_s:
                hits.popleft()
            if not alert_now and len(hits) <= self._threshold:
                self._prune(now)
                return None
            last = self._last.get(key)
            if last is not None and now - last < self._cooldown_s:
                self._repeats[key] = self._repeats.get(key, 0) + 1
                return None
            repeats = self._repeats.pop(key, 0)
            self._last[key] = now
            self._prune(now)
            return repeats

    def _prune(self, now: float) -> None:
        if len(self._last) <= _MAX_TRACKED:
            return
        self._last = {k: t for k, t in self._last.items() if now - t < self._cooldown_s}
        self._repeats = {k: n for k, n in self._repeats.items() if k in self._last}
        self._hits = {k: h for k, h in self._hits.items() if h and now - h[-1] < self._window_s}


class SharedRepeatGate:
    """The same decision as RepeatGate, with state in Redis so every process sees one gate.

    The hit window is fixed rather than sliding, which is close enough for a
    persistence threshold. Use RepeatGate when Redis is unavailable.
    """

    def __init__(self, redis_client, threshold: int = REPEAT_THRESHOLD,
                 window_s: float = WINDOW_S, cooldown_s: float = COOLDOWN_S,
                 prefix: str = "ramboq:err_gate:"):
        self._r = redis_client
        self._threshold = threshold
        self._window_s = int(window_s)
        self._cooldown_s = int(cooldown_s)
        self._prefix = prefix

    def decide(self, key: str, alert_now: bool = False) -> int | None:
        import hashlib
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        hits_key = f"{self._prefix}hits:{digest}"
        cool_key = f"{self._prefix}cool:{digest}"
        rep_key = f"{self._prefix}rep:{digest}"
        hits = self._r.incr(hits_key)
        if hits == 1:
            self._r.expire(hits_key, self._window_s)
        if not alert_now and hits <= self._threshold:
            return None
        if not self._r.set(cool_key, 1, nx=True, ex=self._cooldown_s):
            self._r.incr(rep_key)
            return None
        return int(self._r.getdel(rep_key) or 0)

