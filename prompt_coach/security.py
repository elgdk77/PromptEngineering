"""Request checks that stop other websites from driving the local server.

The app has no login: anything that can reach the port can use it. Browsers
already stop ordinary cross-site requests from reading responses or sending JSON,
but two gaps remain, and this middleware closes both:

- DNS rebinding. An attacker's domain re-resolves to 127.0.0.1, so their page
  becomes "same-origin" with this server. Its requests still carry the attacker's
  hostname in the Host header, so we accept only the hosts we expect.
- Cross-site writes. For any method that changes state, a browser-sent Origin
  header must match the Host the request was sent to.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def hostname_of(host_header: str) -> str:
    """Return the host part of a Host header, without the port, lowercased."""
    host = host_header.strip().lower()
    if host.startswith("["):  # IPv6 literal: [::1]:8000
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.split(":", 1)[0]


def check_request(
    method: str, host: str | None, origin: str | None, allowed_hosts: frozenset[str]
) -> str | None:
    """Return why the request should be refused, or None to let it through."""
    if not host or hostname_of(host) not in allowed_hosts:
        return "Host not allowed."
    if method.upper() in SAFE_METHODS or origin is None:
        # Browsers always send Origin on cross-origin writes; a missing one is a
        # same-origin request from an older browser or a non-browser client.
        return None
    parts = urlsplit(origin)
    if not parts.scheme or parts.netloc.lower() != host.strip().lower():
        return "Cross-origin request refused."
    return None


class LocalOnlyMiddleware:
    """ASGI middleware that applies check_request to every HTTP request."""

    def __init__(self, app, allowed_hosts: frozenset[str]):
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        reason = check_request(
            scope["method"], headers.get("host"), headers.get("origin"), self.allowed_hosts
        )
        if reason is None:
            return await self.app(scope, receive, send)

        body = json.dumps({"detail": reason}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
