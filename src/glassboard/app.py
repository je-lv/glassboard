"""Glassboard HTTP service. `/mcp` only with the optional extra AND GLASSBOARD_MCP=1
(see mcp_adapter.py).

Middleware, outermost first: request context (request ID, security headers, access log without
bodies or credentials) → Host allow-list (DNS-rebinding defence) → body limit (streamed bytes).
"""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import json
import time
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, api_artifacts, api_board, api_comm, api_coord, api_join, api_checkpoints, api_roles, backup, api_me, api_messages, api_sessions, api_sync, api_tasks, dashboard, db, events, journal, run_browser, tokens
from . import longpoll, mcp_adapter, textviews, tlsboot, transport
from .metrics import Metrics
from .sweeper import Sweeper
from . import sessions as session_svc
from .deps import authenticate, get_db
from .errors import (ApiError, AsciiJSONResponse, api_error_handler, http_handler, request_id,
                     unhandled_handler, validation_handler)
from .limits import AuthFailureLimiter, BodyLimitMiddleware, PostRateLimiter
from .protocol import PROTOCOL_TEXT, PROTOCOL_VERSION

log = logging.getLogger("glassboard")
API_VERSION = "v1"
MAX_BODY = 64 * 1024
AUTH_FAILURES_PER_MIN = 10


class ConfigError(RuntimeError):
    """The service refuses to start with this configuration."""


def _allowed_hosts(explicit: list[str] | None) -> list[str]:
    """Outside development mode the Host allow-list must be configured explicitly;
    a wildcard would disable the DNS-rebinding defence and is refused."""
    if explicit is not None:
        hosts = explicit
    elif os.environ.get("GLASSBOARD_ALLOWED_HOSTS", "").strip():
        hosts = [h.strip() for h in os.environ["GLASSBOARD_ALLOWED_HOSTS"].split(",") if h.strip()]
    elif os.environ.get("GLASSBOARD_DEV") == "1":
        hosts = ["localhost", "127.0.0.1"]
    else:
        raise ConfigError("GLASSBOARD_ALLOWED_HOSTS must name the board hostname(s) "
                          "(GLASSBOARD_DEV=1 allows localhost for local development)")
    if not hosts or any("*" in h for h in hosts):
        raise ConfigError("GLASSBOARD_ALLOWED_HOSTS must list concrete hostnames; wildcards are refused")
    return hosts


def create_app(db_path: str | None = None, allowed_hosts: list[str] | None = None,
               journal_path: str | None = None, sweep_interval_s: float | None = None,
               mcp: bool | None = None, plain_http: bool | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("GLASSBOARD_DB", "./var/glassboard.db")
    journal_path = journal_path or journal.default_path(db_path)
    hosts = _allowed_hosts(allowed_hosts)
    use_mcp = mcp_adapter.requested(mcp)
    # Explicit plain-HTTP mode (no TLS, no Caddy). The owner's choice for quick setups;
    # the app then enforces LAN-only itself and warns everywhere.
    plain = plain_http if plain_http is not None else os.environ.get("GLASSBOARD_INSECURE_HTTP") == "1"
    lan_cidrs = transport.allowed_networks(os.environ.get("GLASSBOARD_ALLOWED_CIDRS")) if plain else None
    if plain:
        log.warning("GLASSBOARD_INSECURE_HTTP=1: plain HTTP, NO TLS. Tokens and messages are readable by "
                    "anyone on the LAN. LAN-only is enforced by the app (%s).", " ".join(map(str, lan_cidrs)))
    if use_mcp and not mcp_adapter.available():
        raise ConfigError("GLASSBOARD_MCP=1 but the optional `mcp` package is not installed "
                          "(install requirements/mcp.lock; see docs/deploy.md, \"Optional: MCP\")")

    # Restore needs the instance stopped: a running server holds this lock (shared) until shutdown.
    serving_fd = os.open(backup.serving_lock_path(db_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(serving_fd, fcntl.LOCK_SH)
    database = db.Database(db_path)
    database.migrate()
    # re-apply every journaled access reduction before serving anything. A corrupt
    # journal raises JournalError here and the service does not start; an operator decides.
    journal.replay(database, journal_path)

    if sweep_interval_s is None:
        sweep_interval_s = float(os.environ.get("GLASSBOARD_SWEEP_INTERVAL_S", "60"))
    stale_sweeper = Sweeper(database, sweep_interval_s)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        stale_sweeper.start()           # every 60 s by default; 0 disables
        try:
            if mcp_endpoint is not None:
                async with mcp_endpoint.session_manager.run():   # the SDK needs its task group
                    yield
            else:
                yield
        finally:
            stale_sweeper.stop()
            _app.state.shutdown()

    # the API schema is served only to authenticated callers (route below), so FastAPI's
    # own unauthenticated openapi route is switched off.
    app = FastAPI(title="Glassboard", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan,
                  openapi_url=None, default_response_class=AsciiJSONResponse)
    app.state.sweeper = stale_sweeper
    # Optional local rehearsal boards shown read-only in the Admin console while their agents
    # continue on separate databases. Only the server operator can configure these paths.
    try:
        external = json.loads(os.environ.get("GLASSBOARD_ADMIN_EXTERNAL_DBS", "[]"))
    except json.JSONDecodeError as exc:
        raise ConfigError("GLASSBOARD_ADMIN_EXTERNAL_DBS must be a JSON list of absolute database paths") from exc
    if not isinstance(external, list) or any(not isinstance(path, str) or not os.path.isabs(path) for path in external):
        raise ConfigError("GLASSBOARD_ADMIN_EXTERNAL_DBS must be a JSON list of absolute database paths")
    with database.read() as c:
        saved = c.execute("SELECT value FROM meta WHERE key='admin_external_dbs_override'").fetchone()
    if saved is not None:
        try:
            external = json.loads(saved["value"])
        except json.JSONDecodeError as exc:
            raise ConfigError("Saved separate-board list is not valid JSON") from exc
        if not isinstance(external, list) or any(not isinstance(path, str) or not os.path.isabs(path) for path in external):
            raise ConfigError("Saved separate-board list must contain absolute database paths")
    app.state.admin_external_env_dbs = json.loads(os.environ.get("GLASSBOARD_ADMIN_EXTERNAL_DBS", "[]"))
    app.state.admin_external_dbs = external
    try:
        run_roots = run_browser.configured(os.environ.get("GLASSBOARD_ADMIN_RUN_EVIDENCE_ROOTS", "[]"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    with database.read() as c:
        saved_roots = c.execute("SELECT value FROM meta WHERE key='admin_run_evidence_roots_override'").fetchone()
    if saved_roots is not None:
        try:
            run_roots = run_browser.configured(saved_roots["value"], "Saved run-evidence root list")
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    app.state.admin_run_evidence_env_roots = run_browser.configured(
        os.environ.get("GLASSBOARD_ADMIN_RUN_EVIDENCE_ROOTS", "[]"))
    app.state.admin_run_evidence_roots = run_roots
    app.state.plain_http = plain
    app.state.mcp_enabled = use_mcp
    app.state.sweep_interval_s = sweep_interval_s
    app.state.ca_file = os.environ.get("GLASSBOARD_CA_FILE") or os.path.join(os.path.dirname(os.path.abspath(db_path)), "ca.crt")
    mcp_endpoint = mcp_adapter.McpEndpoint(app, hosts, plain_http=plain) if use_mcp else None

    def shutdown() -> None:
        stale_sweeper.stop()
        database.close()
        try:
            fcntl.flock(serving_fd, fcntl.LOCK_UN)
            os.close(serving_fd)
        except OSError:
            pass                        # already shut down
    app.state.shutdown = shutdown
    app.state.db = database
    app.state.journal_path = journal_path
    app.state.auth_limiter = AuthFailureLimiter(limit=AUTH_FAILURES_PER_MIN, window_s=60.0)
    app.state.post_limiter = PostRateLimiter(per_minute=30, burst=10)
    app.state.metrics = metrics = Metrics()
    app.state.waiters = waiters = longpoll.Waiters()
    database.event_listeners.append(waiters.on_event)
    app.state.allowed_hosts = hosts
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_handler)
    app.add_exception_handler(Exception, unhandled_handler)

    # Added innermost first: Starlette wraps each new middleware around the previous ones.
    app.add_middleware(BodyLimitMiddleware, max_body=MAX_BODY, artifact_max_body=api_artifacts.MAX_ARTIFACT_BYTES)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    if plain:
        app.add_middleware(transport.LanOnlyMiddleware, networks=lan_cidrs)   # outside the Host check

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request_id(request)
        t0 = time.perf_counter()
        plain_token = dashboard.PLAIN_HTTP.set(plain)
        try:
            response = await call_next(request)
        finally:
            dashboard.PLAIN_HTTP.reset(plain_token)
        response.headers["X-Request-Id"] = rid
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        route = request.scope.get("route").path if request.scope.get("route") else "-"
        elapsed = time.perf_counter() - t0
        metrics.request(request.method, route, elapsed)
        if response.headers.get("Glassboard-Replayed") == "true":
            metrics.inc("replay_total")
        # Structured access log: never bodies, tokens or Authorization (hard rule 7).
        log.info("rid=%s method=%s route=%s status=%s ms=%.1f principal=%s", rid, request.method, route,
                 response.status_code, elapsed * 1000, getattr(request.state, "principal_id", "-"))
        return response

    @app.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready(request: Request, database: db.Database = Depends(get_db)):
        """For local probes only. Through Caddy the client address is the real LAN client
        (--proxy-headers), so only a direct loopback request gets an answer; everyone else 404."""
        if (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
            raise ApiError(404, "not_found", "Not found.")
        with database.read() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ready"}

    @app.get(f"/api/{API_VERSION}/openapi.json", include_in_schema=False)
    def openapi_schema(p: tokens.Principal = Depends(authenticate)):
        return app.openapi()

    @app.get(f"/api/{API_VERSION}/discovery")
    def discovery():
        # Only what is implemented: features and limits appear as they land.
        d = {
            "service": "glassboard", "server_version": __version__,
            "api_version": API_VERSION, "protocol_version": PROTOCOL_VERSION,
            "auth": {"scheme": "bearer", "header": "Authorization", "token_prefix": "gbt_",
                     "notes": "One token per principal. Store in a 0600 file; tokens are shown once."},
            "features": ["whoami", "protocol", "sessions", "members", "me.agents", "me.tokens",
                         "messages", "questions", "sync", "history", "export", "tasks", "checkpoints",
                         "board", "attention", "overview", "checklists", "dependencies", "drafts", "priority",
                         "digest", "decisions", "standup", "leases", "approvals", "review", "handoff", "flow", "invites",
                         "artifacts"],
            "limits": {"max_body_bytes": MAX_BODY, "auth_failures_per_min_per_ip": AUTH_FAILURES_PER_MIN,
                       "message_body_bytes": api_messages.MAX_BODY_BYTES, "recipients_per_message": api_messages.MAX_RECIPIENTS,
                       "refs_per_event": 10, "page_size_default": 50, "page_size_max": 100,
                       "agent_posts_per_min": 30, "agent_post_burst": 10, "sync_wait_max_s": 25,
                       "session_max_events": events.MAX_EVENTS, "session_max_bytes": events.MAX_BYTES,
                       "checkpoint_total_bytes": api_checkpoints.MAX_TOTAL, "checkpoint_summary_bytes": api_checkpoints.MAX_SUMMARY,
                       "artifact_bytes": api_artifacts.MAX_ARTIFACT_BYTES,
                       "session_artifact_bytes": api_artifacts.MAX_SESSION_ARTIFACT_BYTES},
        }
        if plain:
            d["transport"] = "plain-http"
            d["warning"] = transport.PLAIN_WARNING
        if mcp_endpoint is not None:
            d["features"].append("mcp")
            d["limits"]["mcp_sync_wait_max_s"] = mcp_adapter.MCP_SYNC_WAIT_MAX_S
        return d

    @app.get("/ca.crt", include_in_schema=False)
    def ca_certificate():
        """The board's root certificate is public (it is what clients pin). Served only if the
        configured file is exactly one PEM certificate, so a misconfigured path can never serve a key."""
        try:
            with open(app.state.ca_file, encoding="ascii") as f:
                pem = tlsboot.check_ca_pem(f.read(tlsboot.MAX_CA_BYTES + 1))
        except (OSError, UnicodeDecodeError, tlsboot.TlsBootError):
            raise ApiError(404, "not_found", "No CA certificate is published on this board.") from None
        return PlainTextResponse(pem, media_type="application/x-pem-file")

    @app.get("/metrics", include_in_schema=False)
    def metrics_view(p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
        """Admin-only. Bearer auth, so a scraper uses an admin's read token."""
        if not p.is_admin:
            raise ApiError(403, "forbidden", "Only instance admins may read metrics.")
        with database.read() as c:
            return PlainTextResponse(metrics.render(c), media_type="text/plain; version=0.0.4")

    @app.get(f"/api/{API_VERSION}/protocol")
    def protocol():
        return PlainTextResponse(PROTOCOL_TEXT, media_type="text/markdown; charset=utf-8")

    @app.get(f"/api/{API_VERSION}/whoami")
    def whoami(format: Literal["json", "text"] = Query("json"),
               p: tokens.Principal = Depends(authenticate), database: db.Database = Depends(get_db)):
        with database.read() as c:
            sessions = session_svc.my_sessions(c, p.id)
        data = {"handle": p.handle, "kind": p.kind, "owner": p.owner_handle, "is_admin": p.is_admin,
                "credential_id": p.credential_id, "scope": p.scope, "expires_at": p.expires_at,
                "sessions": sessions}
        if format == "text":
            return PlainTextResponse(textviews.whoami_text(data))
        return data

    app.include_router(api_sessions.router)
    app.include_router(api_me.router)
    app.include_router(api_messages.router)
    app.include_router(api_sync.router)
    app.include_router(api_tasks.router)
    app.include_router(api_artifacts.router)
    app.include_router(api_checkpoints.router)
    app.include_router(api_board.router)
    app.include_router(api_comm.router)
    app.include_router(api_coord.router)
    app.include_router(api_join.router)
    app.include_router(api_roles.router)
    app.include_router(api_roles.session_router)
    dashboard.mount(app)
    if mcp_endpoint is not None:
        app.add_route("/mcp", mcp_endpoint, methods=["GET", "POST", "DELETE"], include_in_schema=False)
    return app
