"""Forward ERROR log records to ntfy and Telegram, one alert per distinct error.

Attached to the queue listener in ramboq_logger, so it sees every record.
Delivery runs on a worker thread, so logging never waits on the network.

Classification: an ERROR alerts only when its message repeats more than
REPEAT_THRESHOLD times in WINDOW_S (a persistent failure). Earlier repeats
are treated as warnings and are not sent. A record logged with
``extra={"alert_now": True}`` (a failure with no automatic recovery) alerts
on its first occurrence. A given message alerts at most once per COOLDOWN_S.
"""
import html
import logging
import queue
import re
import threading
import time
from collections import deque

from backend.shared.helpers.text_clean import to_plain

COOLDOWN_S = 900
WINDOW_S = 900
REPEAT_THRESHOLD = 3
_MAX_PENDING = 200
_MAX_MSG_CHARS = 300
_MAX_TRACKED = 1000
_SKIP_LOGGERS = (
    "backend.shared.helpers.error_alerts",
    "backend.shared.helpers.alert_utils",
)
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


def _deliver_now(name: str, msg: str, repeats: int) -> None:
    from backend.shared.helpers.alert_utils import _send_telegram, send_ntfy_alert
    from backend.shared.helpers.utils import is_enabled

    want_ntfy = is_enabled("ntfy")
    want_tg = is_enabled("telegram")
    if not (want_ntfy or want_tg):
        return
    suffix = f" (+{repeats} repeats)" if repeats else ""
    if want_ntfy:
        send_ntfy_alert("RamboQuant error", f"{name}\n{msg}{suffix}")
    if want_tg:
        _send_telegram(
            f"<b>RamboQuant error</b>\n<code>{html.escape(name)}</code>\n"
            f"{html.escape(msg)}{html.escape(suffix)}",
        )


class ErrorAlertHandler(logging.Handler):
    def __init__(self, deliver=_deliver_now, clock=time.monotonic):
        super().__init__(level=logging.ERROR)
        self._deliver = deliver
        self._clock = clock
        self._last: dict[str, float] = {}
        self._repeats: dict[str, int] = {}
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=_MAX_PENDING)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="error-alerts").start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_SKIP_LOGGERS):
                return
            from backend.shared.helpers.utils import mask_account_in_text

            msg = clean_message(mask_account_in_text(record.getMessage()) or "")
            key = f"{record.name}|{msg}"
            now = self._clock()
            alert_now = bool(getattr(record, "alert_now", False))
            with self._lock:
                hits = self._hits.setdefault(key, deque())
                hits.append(now)
                while hits and now - hits[0] > WINDOW_S:
                    hits.popleft()
                if not alert_now and len(hits) <= REPEAT_THRESHOLD:
                    self._prune(now)
                    return
                last = self._last.get(key)
                if last is not None and now - last < COOLDOWN_S:
                    self._repeats[key] = self._repeats.get(key, 0) + 1
                    return
                repeats = self._repeats.pop(key, 0)
                self._last[key] = now
                self._prune(now)
            self._q.put_nowait((record.name, msg, repeats))
        except Exception:
            pass

    def _prune(self, now: float) -> None:
        if len(self._last) <= _MAX_TRACKED:
            return
        self._last = {k: t for k, t in self._last.items() if now - t < COOLDOWN_S}
        self._repeats = {k: n for k, n in self._repeats.items() if k in self._last}
        self._hits = {k: h for k, h in self._hits.items() if h and now - h[-1] < WINDOW_S}

    def _deliver_safely(self, item: tuple) -> None:
        try:
            self._deliver(*item)
        except Exception:
            pass

    def _run(self) -> None:
        while True:
            self._deliver_safely(self._q.get())
