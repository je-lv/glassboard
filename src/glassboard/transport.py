"""Plain-HTTP mode, chosen by the owner for quick setups.

Without Caddy nothing else enforces LAN-only, so the app does it itself: a request
whose client address is outside the allowed networks gets 403 before any routing or authentication.
The defaults equal Caddy's `private_ranges`, the same default as deploy/Caddyfile. The app binds
the LAN address directly in this mode, without --proxy-headers, so the client address is the real
peer.
"""
from __future__ import annotations

import ipaddress

from .errors import AsciiJSONResponse

PRIVATE_RANGES = "192.168.0.0/16 172.16.0.0/12 10.0.0.0/8 127.0.0.0/8 fd00::/8 ::1/128"
PLAIN_WARNING = ("This board runs over plain HTTP without TLS: anyone on the LAN can read tokens and "
                 "messages and act as any participant whose token they capture.")


def allowed_networks(spec: str | None) -> list:
    return [ipaddress.ip_network(x, strict=False) for x in (spec or PRIVATE_RANGES).replace(",", " ").split()]


class LanOnlyMiddleware:
    def __init__(self, app, networks: list):
        self.app, self.networks = app, networks

    def _allowed(self, host: str | None) -> bool:
        try:
            ip = ipaddress.ip_address(host or "")
        except ValueError:
            return False
        return any(ip in n for n in self.networks if n.version == ip.version)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and not self._allowed((scope.get("client") or (None,))[0]):
            body = {"error": {"code": "lan_only", "message": "This board answers only its local network.",
                              "retryable": False}}
            await AsciiJSONResponse(body, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)
