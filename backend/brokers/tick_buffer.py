"""Shared-memory tick buffer — KiteTicker → mmap → main API readers.

Lives at /dev/shm/ramboq_ticks (RAM-backed). Conn_service is the
single writer; ramboq_api is a read-only consumer. No locks: writers
bump a monotonic version word AFTER finishing the slot write, readers
detect a torn read by re-checking the version. Single writer means
torn reads are bounded — at worst the reader sees a partial update
of one slot and retries; it never sees an inconsistent slot-table-
wide state.

Layout (fixed at HEADER_SIZE + max_slots × SLOT_SIZE bytes):

  HEADER (64 B)
    uint64  version       — monotonic, bumped on every tick
    uint32  slot_count    — currently occupied slots
    uint32  max_slots     — capacity (4096 default)
    uint64  last_write_ns — monotonic ns of most recent write
    uint8   schema_version — bump on incompatible layout change
    uint8[39] reserved    — pad to 64 B

  SLOT_TABLE (max_slots × 40 B)
    uint32  token         — Kite instrument_token (0 = empty)
    uint32  _pad
    double  last_price
    double  prev_close
    double  avg_price
    uint64  last_ts_ns

Indexing: hash(token) % max_slots, linear probe on collision. The
collision rate is negligible up to ~70% load (we target ≤4096 slots,
typical subscriptions ~300).

Why not a queue: readers need O(1) random-access by token, not FIFO.
Why not a dict over UDS: every tick read in routes/quote.py and
SSE-emit paths fires hundreds of times per second. A mmap byte read
is nanoseconds; a UDS round-trip is milliseconds.
"""

from __future__ import annotations

import mmap
import os
import struct
from typing import Iterator

# Tunables — keep in sync between writer + reader processes. Bumping
# SCHEMA_VERSION forces both sides to recreate the buffer.
SCHEMA_VERSION = 1
DEFAULT_MAX_SLOTS = 4096
DEFAULT_PATH = "/dev/shm/ramboq_ticks"

# struct formats — little-endian for x86/ARM, doesn't matter as both
# halves run on the same host.
_HEADER_FMT = "<QIIQB39x"   # version, slot_count, max_slots, last_write_ns, schema_version, pad
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
assert _HEADER_SIZE == 64, _HEADER_SIZE

_SLOT_FMT = "<II3dQ"        # token, _pad, last_price, prev_close, avg_price, last_ts_ns
_SLOT_SIZE = struct.calcsize(_SLOT_FMT)
assert _SLOT_SIZE == 40, _SLOT_SIZE


def _buffer_size(max_slots: int) -> int:
    return _HEADER_SIZE + max_slots * _SLOT_SIZE


# ── Writer ────────────────────────────────────────────────────────────


class TickBufferWriter:
    """Single-writer ticker buffer. Lives in conn_service process.

    Create with `TickBufferWriter()` — sizes itself from DEFAULT_MAX_SLOTS
    and creates the file if missing. Reset the buffer on construction
    (start of process) so stale tokens from a prior run don't linger.
    """

    def __init__(self, path: str = DEFAULT_PATH, max_slots: int = DEFAULT_MAX_SLOTS):
        self.path = path
        self.max_slots = max_slots
        size = _buffer_size(max_slots)
        # Open/create + size + map. We always re-truncate on writer
        # boot so the schema is freshly laid out and the slot table
        # starts empty.
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
        try:
            os.ftruncate(fd, size)
            self._mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        finally:
            os.close(fd)
        self._reset()

    def _reset(self) -> None:
        """Zero the whole buffer and stamp the schema version."""
        self._mm[:] = b"\x00" * len(self._mm)
        # version=0, slot_count=0, max_slots, last_write_ns=0, schema_version
        struct.pack_into(
            _HEADER_FMT, self._mm, 0,
            0, 0, self.max_slots, 0, SCHEMA_VERSION,
        )

    def _slot_index(self, token: int) -> int:
        """Linear-probe index for `token`. Returns the slot whose
        token == this token (existing entry) OR the first empty slot
        on the probe chain (new entry). Returns -1 if the table is
        full (cannot happen at <70% load, but checked defensively)."""
        slot_count_off = _HEADER_SIZE
        i = token % self.max_slots
        for _ in range(self.max_slots):
            off = slot_count_off + i * _SLOT_SIZE
            cur_token = struct.unpack_from("<I", self._mm, off)[0]
            if cur_token == token or cur_token == 0:
                return i
            i = (i + 1) % self.max_slots
        return -1

    def upsert(
        self,
        token: int,
        last_price: float,
        prev_close: float = 0.0,
        avg_price: float = 0.0,
        ts_ns: int = 0,
    ) -> bool:
        """Write or update the slot for `token`. Returns False when
        the table is full (writer-side decision: log + drop)."""
        idx = self._slot_index(token)
        if idx < 0:
            return False
        off = _HEADER_SIZE + idx * _SLOT_SIZE
        # Detect new-slot insertion to bump slot_count.
        cur_token = struct.unpack_from("<I", self._mm, off)[0]
        is_new = cur_token == 0
        struct.pack_into(
            _SLOT_FMT, self._mm, off,
            token, 0, last_price, prev_close, avg_price, ts_ns,
        )
        # Bump the version word LAST so readers either see the old
        # version (and the old slot) or the new version (and the new
        # slot). Single writer means no CAS needed.
        version_off = 0
        cur_version = struct.unpack_from("<Q", self._mm, version_off)[0]
        struct.pack_into("<Q", self._mm, version_off, cur_version + 1)
        if is_new:
            slot_count_off = 8  # immediately after version
            cur_count = struct.unpack_from("<I", self._mm, slot_count_off)[0]
            struct.pack_into("<I", self._mm, slot_count_off, cur_count + 1)
        # Stamp last_write_ns for staleness monitoring.
        if ts_ns:
            struct.pack_into("<Q", self._mm, 16, ts_ns)
        return True

    def close(self) -> None:
        self._mm.close()


# ── Reader ────────────────────────────────────────────────────────────


class TickBufferReader:
    """Read-only mmap reader. Many instances OK (e.g. SSE + each
    route handler can hold one). Opens RO mmap, never modifies.

    `get_ltp(token)` is the hot path — single struct.unpack at a
    computed offset, no locks, no allocation."""

    def __init__(self, path: str = DEFAULT_PATH, max_slots: int = DEFAULT_MAX_SLOTS):
        self.path = path
        self.max_slots = max_slots
        size = _buffer_size(max_slots)
        # The writer truncates on boot, so as long as the writer is
        # up the file exists at the right size. If the writer hasn't
        # booted yet, mmap() raises; caller can retry.
        fd = os.open(path, os.O_RDONLY)
        try:
            self._mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ)
        finally:
            os.close(fd)

    def header(self) -> tuple[int, int, int, int, int]:
        """Returns (version, slot_count, max_slots, last_write_ns,
        schema_version). Cheap — single 64-byte read."""
        return struct.unpack_from(_HEADER_FMT, self._mm, 0)

    def version(self) -> int:
        """Read just the version word. Used by SSE poller to detect
        changes without unpacking the whole header."""
        return struct.unpack_from("<Q", self._mm, 0)[0]

    def get_ltp(self, token: int) -> float | None:
        """Look up `token`'s last_price. None when not subscribed.

        Torn-read protection: re-read the version word before and
        after the slot read; if it changed during the read AND the
        slot's token doesn't match, retry once. Single retry is
        sufficient because the writer never holds a slot in a torn
        state for longer than one struct.pack_into call (~µs)."""
        slot_count_off = _HEADER_SIZE
        for _ in range(2):  # at most one retry
            i = token % self.max_slots
            for _ in range(self.max_slots):
                off = slot_count_off + i * _SLOT_SIZE
                cur_token, _pad, lp, _pc, _av, _ts = struct.unpack_from(
                    _SLOT_FMT, self._mm, off,
                )
                if cur_token == token:
                    return lp
                if cur_token == 0:
                    return None
                i = (i + 1) % self.max_slots
            return None
        return None

    def get_ltp_batch(self, tokens: list[int]) -> dict[int, float]:
        """Batch LTP read. Same scan per token; could be optimized
        further with a single linear sweep + dict lookup, but the
        current cost is already ~ns per token so not worth it yet."""
        out: dict[int, float] = {}
        for t in tokens:
            v = self.get_ltp(t)
            if v is not None:
                out[t] = v
        return out

    def iter_active(self) -> Iterator[tuple[int, float, float, float, int]]:
        """Yield (token, last_price, prev_close, avg_price, last_ts_ns)
        for every occupied slot. Used by SSE fan-out + diagnostics.

        Early-exit on slot_count — header records exactly how many
        slots are occupied. Without this, every call scans all 4096
        slots even when ~300 are populated (93% wasted struct.unpacks).
        SSE poll path is version-gated so cost is bounded by tick rate,
        but at 10-20 ticks/sec we still spared 41k-82k pointless reads."""
        # Header layout: version(8) + slot_count(4) at offset 8.
        slot_count = struct.unpack_from("<I", self._mm, 8)[0]
        if slot_count == 0:
            return
        slot_base = _HEADER_SIZE
        seen = 0
        for i in range(self.max_slots):
            off = slot_base + i * _SLOT_SIZE
            cur_token, _pad, lp, pc, av, ts = struct.unpack_from(
                _SLOT_FMT, self._mm, off,
            )
            if cur_token != 0:
                yield cur_token, lp, pc, av, ts
                seen += 1
                if seen >= slot_count:
                    return

    def close(self) -> None:
        self._mm.close()
