"""Forward ERROR log records to ntfy and Telegram, one alert per distinct error.

Attached to the queue listener in ramboq_logger, so it sees every record.
Delivery runs on a worker thread, so logging never waits on the network.
A given (logger, message) alerts at most once per COOLDOWN_S; repeats are
counted and reported with the next alert for that message.
"""
import html
import logging
import queue
import threading
import time

COOLDOWN_S = 900
_MAX_PENDING = 200
_MAX_MSG_CHARS = 1000
_MAX_TRACKED = 1000
_SKIP_LOGGERS = (
    "backend.shared.helpers.error_alerts",
    "backend.shared.helpers.alert_utils",
)


def _deliver_now(name: str, msg: str, repeats: int) -> None:
    from backend.shared.helpers.alert_utils import _send_telegram, send_ntfy_alert
    from backend.shared.helpers.utils import is_enabled

    want_ntfy = is_enabled("ntfy")
    want_tg = is_enabled("telegram")
    if not (want_ntfy or want_tg):
        return
    suffix = f"\n(+{repeats} repeats)" if repeats else ""
    body = f"{name}\n{msg}{suffix}"
    if want_ntfy:
        send_ntfy_alert("RamboQuant error", body)
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
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=_MAX_PENDING)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="error-alerts").start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_SKIP_LOGGERS):
                return
            from backend.shared.helpers.utils import mask_account_in_text

            msg = (mask_account_in_text(record.getMessage()) or "")[:_MAX_MSG_CHARS]
            key = f"{record.name}|{msg}"
            now = self._clock()
            with self._lock:
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

    def _deliver_safely(self, item: tuple) -> None:
        try:
            self._deliver(*item)
        except Exception:
            pass

    def _run(self) -> None:
        while True:
            self._deliver_safely(self._q.get())
