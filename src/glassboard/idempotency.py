"""Idempotency keys, request digests and receipts.

Digest: req_digest = SHA-256(canonical_json(D)) with
    D = {"v": 1, "operation": "<METHOD> <route template>", "session_id": <id or null>,
         "target": {"type": ..., "id": ...} or null, "actor_id": <principal id>,
         "runtime": <label or null>, "payload": <validated model, defaults applied>}
Canonical JSON: sorted keys, separators (",", ":"), UTF-8 without ASCII escaping (bytes matter,
no Unicode normalisation). Callers resolve handles to immutable IDs and sort recipient lists
BEFORE building the payload. `runtime` is bound only for claim, reclaim, sync attach and takeover;
everywhere else the caller passes None.

Receipts: the exact response body bytes are stored once at commit and replayed
byte-identical with the original success status and the header `Glassboard-Replayed: true`.
"""
from __future__ import annotations

import hashlib
import json
import re

from fastapi import Header
from fastapi.responses import Response

from .errors import ApiError

# Keys are opaque client strings: ULIDs from our CLI, anything reasonable from other clients.
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
REPLAY_HEADER = "Glassboard-Replayed"


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(operation: str, actor_id: str, payload: dict, session_id: str | None = None,
           target: dict | None = None, runtime: str | None = None) -> bytes:
    d = {"v": 1, "operation": operation, "session_id": session_id, "target": target,
         "actor_id": actor_id, "runtime": runtime, "payload": payload}
    return hashlib.sha256(canonical(d)).digest()


def idempotency_key(idempotency_key: str = Header(..., alias="Idempotency-Key")) -> str:
    """Required on every mutation. Structural: rejected, never rendered."""
    if not KEY_RE.match(idempotency_key):
        raise ApiError(422, "invalid_request", "The request failed validation.",
                       details={"fields": [{"loc": ["header", "idempotency-key"], "type": "string_pattern_mismatch"}]})
    return idempotency_key


def receipt_body(obj: dict) -> str:
    """The exact JSON text that is sent and stored. ASCII-only."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True)


def respond(body: str, status: int, replayed: bool = False) -> Response:
    headers = {REPLAY_HEADER: "true"} if replayed else None
    return Response(content=body, status_code=status, media_type="application/json", headers=headers)


def check_replay(row, req_digest: bytes, status: int) -> Response | None:
    """`row` is the stored record for (scope, actor, key) or None. Same digest → replay; different
    digest → 409 idempotency_key_reused; no row → None (proceed)."""
    if row is None:
        return None
    if bytes(row["req_digest"] or b"") != req_digest:
        raise ApiError(409, "idempotency_key_reused",
                       "This Idempotency-Key was already used for a different request.",
                       hint="Use a new key for a new operation.")
    return respond(row["receipt"], status, replayed=True)
