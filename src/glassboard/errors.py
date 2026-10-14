"""Error envelope and handlers that never echo client input."""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("glassboard")


class AsciiJSONResponse(JSONResponse):
    """All JSON output is ASCII; control, bidi and other non-ASCII characters reach clients
    only as \\uXXXX escapes. Starlette's JSONResponse uses ensure_ascii=False, so it is the
    application's default response class instead (app.py)."""

    def render(self, content) -> bytes:
        return json.dumps(content, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
_SAFE_SEG = __import__("re").compile(r"^[a-z_][a-z0-9_]{0,31}$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str | None = None,
                 retryable: bool = False, details: dict | None = None, headers: dict | None = None):
        self.status, self.code, self.message, self.hint = status, code, message, hint
        self.retryable, self.details, self.headers = retryable, details or {}, headers or {}


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = "req_" + uuid.uuid4().hex[:16]
        request.state.request_id = rid
    return rid


def wants_text(request: Request) -> bool:
    """`?format=text` or `Accept: text/plain` selects canonical text, for errors too."""
    if request.query_params.get("format") == "text":
        return True
    accept = request.headers.get("accept", "")
    return accept.split(",")[0].strip().startswith("text/plain")


def envelope(request: Request, status: int, code: str, message: str, hint=None,
             retryable=False, details=None, headers=None):
    if wants_text(request):
        from .render import error_lines   # render imports nothing from here; local import keeps it light
        text = "\n".join(error_lines(code, message, hint, details, request_id(request))) + "\n"
        return PlainTextResponse(text, status_code=status, headers=headers)
    if ("text/html" in request.headers.get("accept", "") and
            not request.url.path.startswith(("/api/", "/mcp"))):
        from .dashboard import page
        response = page("error.html", status=status, viewer=None, status_code=status, code=code,
                        message=message, request_id=request_id(request), retryable=retryable)
        if headers:
            response.headers.update(headers)
        return response
    body = {"error": {"code": code, "message": message, "request_id": request_id(request),
                      "retryable": retryable}}
    if hint:
        body["error"]["hint"] = hint
    if details:
        body["error"]["details"] = details
    return AsciiJSONResponse(body, status_code=status, headers=headers)


def _metrics(request: Request):
    return getattr(request.app.state, "metrics", None)


async def api_error_handler(request: Request, exc: ApiError):
    m = _metrics(request)
    if m is not None:
        m.error(exc.code)
    return envelope(request, exc.status, exc.code, exc.message, exc.hint, exc.retryable,
                    exc.details, exc.headers)


def safe_fields(errors, prefix: tuple = ()) -> list[dict]:
    """Field paths and error types ONLY, never the client's input (also used for MCP tool arguments)."""
    fields = []
    for e in list(errors)[:20]:
        etype = str(e.get("type", "invalid"))[:64]
        loc = list(prefix)
        for p in e.get("loc", ()):
            if isinstance(p, int):
                loc.append(p)
            elif isinstance(p, str) and _SAFE_SEG.match(p):
                loc.append(p)
            else:
                loc.append("?")
        if etype == "extra_forbidden" and len(loc) > len(prefix):
            loc[-1] = "<unexpected field>"   # the key itself is client-controlled
        fields.append({"loc": loc, "type": etype})
    return fields


async def validation_handler(request: Request, exc: RequestValidationError):
    # Errors never echo client input: return field paths and error types ONLY. FastAPI's default
    # includes "input" and "msg" (msg can embed the offending value, e.g. enum/literal errors), so
    # neither is returned.
    fields = safe_fields(exc.errors())
    return envelope(request, 422, "invalid_request", "The request failed validation.",
                    hint="See details.fields for the failing locations.", details={"fields": fields})


_HTTP_CODES = {404: ("not_found", "Not found."), 405: ("method_not_allowed", "Method not allowed."),
               400: ("bad_request", "Bad request."), 413: ("payload_too_large", "Payload too large.")}


async def http_handler(request: Request, exc: StarletteHTTPException):
    code, msg = _HTTP_CODES.get(exc.status_code, ("http_error", "Request could not be processed."))
    headers = {"Allow": exc.headers["Allow"]} if exc.headers and "Allow" in exc.headers else None
    return envelope(request, exc.status_code, code, msg, headers=headers)


async def unhandled_handler(request: Request, exc: Exception):
    m = _metrics(request)
    if m is not None and "database is locked" in str(exc):
        m.inc("sqlite_busy_total")
    log.exception("unhandled error request_id=%s", request_id(request))
    return envelope(request, 500, "internal", "Internal error.", retryable=False)
