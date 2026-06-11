"""
Async queue-based logging for RamboQuant.

The short-log files (`.log/short_*`) that shipped earlier — each rewritten
in full on every record — were dropped because they burned dozens of sync
I/O round-trips per minute during alert bursts. What remains:

  - `log_file`  (5 MB × 5 backups)  — full app log, rotating
  - `error_file` (5 KB × 5 backups) — errors only, rotating
  - console (stderr)                — honours CONSOLE_LOG_LEVEL

`/api/admin/logs` reads `log_file` directly via `tail`, so nothing
downstream depended on the short-log files.
"""

import logging
import os
import queue
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

import yaml

# Load configuration from YAML file (resolve relative to repo root)
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "backend" / "config" / "backend_config.yaml"
with open(_CONFIG_PATH, 'r', encoding='utf-8', errors='ignore') as file:
    deploy = yaml.safe_load(file)

# Optional prefix for log file names (set via RAMBOQ_LOG_PREFIX env var)
# Allows different API processes to write to distinct log files
_LOG_PREFIX = os.environ.get('RAMBOQ_LOG_PREFIX', '')

# Extract log settings from config
FILE_LOG_FILE = os.path.join(os.path.dirname(deploy['file_log_file']), _LOG_PREFIX + os.path.basename(deploy['file_log_file']))
ERROR_LOG_FILE = os.path.join(os.path.dirname(deploy['error_log_file']), _LOG_PREFIX + os.path.basename(deploy['error_log_file']))
FILE_LOG_LEVEL = int(deploy['file_log_level'])
ERROR_LOG_LEVEL = int(deploy['error_log_level'])
CONSOLE_LOG_LEVEL = int(deploy['console_log_level'])


# Ensure log directory exists
os.makedirs(os.path.dirname(FILE_LOG_FILE), exist_ok=True)

# Create global log queue for asynchronous logging
log_queue = queue.Queue()

# Common formatter for all handlers
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# --- Rotating File Handlers ---
log_file_handler = RotatingFileHandler(
    FILE_LOG_FILE,
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=5, encoding="utf-8"
)
log_file_handler.setLevel(FILE_LOG_LEVEL)
log_file_handler.setFormatter(formatter)

error_file_handler = RotatingFileHandler(
    ERROR_LOG_FILE,
    maxBytes=5 * 1024,  # ~5 KB (small log, only errors)
    backupCount=5,
    encoding="utf-8"
)
error_file_handler.setLevel(ERROR_LOG_LEVEL)
error_file_handler.setFormatter(formatter)

# --- Console Handler ---
console_handler = logging.StreamHandler()
console_handler.setLevel(CONSOLE_LOG_LEVEL)
console_handler.setFormatter(formatter)

# --- Queue Listener ---
# Processes logs asynchronously from queue to all handlers
queue_listener = QueueListener(
    log_queue,
    console_handler,
    log_file_handler,
    error_file_handler,
    respect_handler_level=True
)
queue_listener.start()


def get_logger(name="app_logger"):
    """
    Return a logger instance with our queue handler attached.

    Important: we check the LOGGER'S OWN handlers, not `hasHandlers()`,
    because `hasHandlers()` walks the parent chain and returns True the
    moment ANY ancestor (root, "backend", etc.) has a handler.

    With the previous `if not logger.hasHandlers():` guard, the FIRST
    call to get_logger("backend.x.y") would install the queue handler
    on that logger. A SUBSEQUENT call to get_logger("backend.a.b") would
    see "backend.x.y" already has a handler — `hasHandlers()` on
    "backend.a.b" walks up to root (still no handler) so returns False,
    which is fine — BUT if uvicorn (or any library) installed a root
    handler at import time, every later get_logger call short-circuits,
    the child logger has no own handler, propagation routes records to
    the parent's handler at parent's level (WARNING by default), and
    every `logger.info(...)` from that module is silently dropped.

    This was the observed prod symptom: `backend.shared.helpers.kite_ticker`
    never wrote a single line to api_log_file across every deploy
    on 2026-06-10/11, while `backend.shared.helpers.connections` (which
    happens to be imported earlier) wrote lines normally. The fix is to
    check this specific logger's `.handlers` list and add the queue
    handler if not already present.
    """
    logger = logging.getLogger(name)
    has_queue_handler = any(isinstance(h, QueueHandler) for h in logger.handlers)
    if not has_queue_handler:
        logger.setLevel(logging.DEBUG)  # Capture everything, handlers filter levels
        logger.addHandler(QueueHandler(log_queue))
        # Suppress propagation to ancestor handlers — otherwise records
        # would be emitted twice (once via our queue, once via whichever
        # parent installed an earlier handler).
        logger.propagate = False
    return logger


def shutdown_logger():
    """Gracefully stop the queue listener (to be called at shutdown)."""
    queue_listener.stop()
