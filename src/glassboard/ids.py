"""Identifiers: all IDs are ULIDs (opaque).

ULID = 48-bit big-endian millisecond Unix timestamp + 80 random bits, encoded as 26 characters of
Crockford base32 (alphabet without I, L, O, U). Lexicographic order follows creation time at
millisecond resolution; within one millisecond the order is random (no monotonic counter).
IDs are opaque to clients: nothing may parse meaning out of them except tests and diagnostics.
Stdlib only, so the participant CLI could share it without new dependencies.
"""
from __future__ import annotations

import re
import secrets
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")   # first char ≤ 7: 128 bits fit in 26 chars
# Fixed ID of the `board` system principal (principal kind 'system'). It is the ULID of timestamp 0 with
# zero randomness, so it can never collide with a generated ID.
BOARD_PRINCIPAL_ID = "0" * 26


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_ulid(ms: int | None = None) -> str:
    """Return a new ULID string. `ms` overrides the timestamp (tests only)."""
    if ms is None:
        ms = time.time_ns() // 1_000_000
    if not 0 <= ms < 1 << 48:
        raise ValueError("timestamp out of ULID range")
    return _encode((ms << 80) | secrets.randbits(80), 26)


def ulid_ms(ulid: str) -> int:
    """Millisecond timestamp encoded in a ULID (diagnostics and tests only)."""
    if not ULID_RE.match(ulid):
        raise ValueError("not a ULID")
    v = 0
    for ch in ulid:
        v = (v << 5) | CROCKFORD.index(ch)
    return v >> 80


def new_random_b32(n_chars: int) -> str:
    """n_chars of Crockford base32 from the OS CSPRNG (5 bits each). Used for recovery IDs."""
    return _encode(secrets.randbits(5 * n_chars), n_chars)
