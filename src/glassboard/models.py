"""Request model base (defence against a spoofed author).

The actor of every request is the authenticated principal, never a body field. Every request body
model subclasses StrictModel, so an unexpected field such as `actor`, `author`, `owner` or
`from` is rejected with 422 instead of silently ignored. tests/test_auth_hardening.py walks every
registered route and fails if a body model does not forbid extra fields.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Ref(StrictModel):
    """An artifact reference. Inert strings: the server never fetches URLs or opens paths.
    `sha256` and `access` are structural: invalid → 422. `label`/`loc` render as Inline."""
    label: str = Field(min_length=1, max_length=128)
    loc: str = Field(min_length=1, max_length=512)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    access: Literal["owner_only", "shared", "unavailable"]


MAX_REFS = 10   # ≤ 10 refs per event
