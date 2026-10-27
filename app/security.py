"""Small security helpers: per-IP rate limits, security headers, and a same-origin check."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_lock = threading.Lock()


def check_rate(request: Request, action: str, limit: int, window_s: int) -> None:
    """Allow `limit` attempts per client IP per window. In memory, so it resets on restart.

    That's fine for one server process; several processes would each keep their own count.
    """
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _lock:
        hits = _hits[(action, ip)]
        while hits and now - hits[0] > window_s:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(429, "Too many attempts. Please wait a few minutes and try again.")
        hits.append(now)


def reset_rate_limits() -> None:
    with _lock:
        _hits.clear()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    # Everything the app loads comes from its own origin; no inline scripts.
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'",
}


def canonical_redirect(request: Request) -> str | None:
    """Where to send a request that arrived on a secondary address, or None to serve it here.

    With CANONICAL_HOST=vibegrind.app, visits to vibegrind.onrender.com and www.vibegrind.app go to
    https://vibegrind.app with the same path. Any other host (e.g. Render's internal health checks or
    localhost) is served normally, and so is /healthz.
    """
    canonical = (os.environ.get("CANONICAL_HOST") or "").strip().lower()
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if not canonical or host == canonical or request.url.path == "/healthz":
        return None
    if host == f"www.{canonical}" or host.endswith(".onrender.com"):
        query = f"?{request.url.query}" if request.url.query else ""
        return f"https://{canonical}{request.url.path}{query}"
    return None


class SecurityMiddleware(BaseHTTPMiddleware):
    """Adds security headers, and rejects state-changing requests sent from other websites.

    The session cookie is SameSite=Lax, which already stops most cross-site requests; checking
    the Origin header on top of that closes the remaining gaps.
    """

    async def dispatch(self, request: Request, call_next):
        target = canonical_redirect(request)
        if target:
            # 301 for normal page loads; 308 keeps the method and body for API calls.
            return RedirectResponse(target, status_code=301 if request.method in ("GET", "HEAD") else 308)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # Make browsers re-check the page and its files on every load (cheap thanks to ETags),
        # so a new deploy shows up right away instead of an old cached copy.
        if request.url.path in ("/", "/app", "/privacy", "/terms", "/imprint") or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response
