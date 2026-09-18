"""SERVER MODE — the login that stands in front of the dashboard on a VPS.

LOCAL MODE (the default) is untouched by this file: ``init_app`` returns before
it registers anything, so there is no /login route, no gate, no cookie flags and
no proxy middleware. The 127.0.0.1 bind is still that mode's whole boundary.

SERVER MODE is switched on by ``ADSPY2_SERVER_MODE=1`` (or, in tests, by
``create_app({"SERVER_MODE": True, ...})``). Behind a reverse proxy EVERY request
arrives from loopback, so "it came from 127.0.0.1" stops meaning "it is the
owner". What replaces it:

  * one admin password (``ADSPY2_ADMIN_PASSWORD``, >= 12 characters) and one
    session-signing key (``ADSPY2_SECRET_KEY``, >= 32 characters). Boot REFUSES
    without them. Only a SHA-256 digest of the password is kept in memory; the
    password itself is never logged and never echoed in an error.
  * a ``before_request`` gate: everything except /login, /logout, /health,
    /static/* and the worker API needs a logged-in session. HTML is redirected
    to ``/login?next=...``; ``/api/*`` gets a JSON 401.
  * the worker API (/api/worker/*, /api/jobs/*, POST /api/logs/worker) stays
    TOKEN-auth — an extension cannot do a cookie login — and in server mode the
    gate itself demands a valid ``X-Worker-Token`` on every one of those calls.
    The token is pasted by the owner, never issued: app/jobs.py's first-contact
    hand-out is disabled too (belt and braces).
  * login attempts are rate-limited per client IP (5 failures / 5 minutes, in
    memory — the launcher runs exactly one worker, so one process sees them all).
  * ``ProxyFix(x_for=1, x_proto=1)``: exactly ONE proxy hop is trusted, so the
    limiter sees the real client and ``request.is_secure`` is true behind Caddy.
  * the session cookie is Secure + HttpOnly + SameSite=Lax. SameSite=Lax is
    also what keeps a cross-site form from driving the POST-only actions.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import sys
import threading
import time
from collections import deque
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    Flask,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

from . import config as app_config

log = logging.getLogger("adspy2.auth")

bp = Blueprint("auth", __name__)

MIN_PASSWORD_CHARS = 12
MIN_SECRET_KEY_CHARS = 32
MAX_PASSWORD_BYTES = 1024            # nobody's password; stops a 50 MB "guess"

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300
SESSION_DAYS = 7

WORKER_TOKEN_HEADER = "X-Worker-Token"

# No session needed. Exact paths, then prefixes.
PUBLIC_PATHS = ("/login", "/logout", "/health")
PUBLIC_PREFIXES = ("/static/",)
# Token-auth, not cookie-auth: the extension's five endpoints, plus the one
# place it ships its log ring buffer (app/routes/logs.py).
WORKER_PREFIXES = ("/api/worker/", "/api/jobs/")
WORKER_PATHS = ("/api/logs/worker",)

_EXT_KEY = "adspy2_auth"


class ServerModeConfigError(RuntimeError):
    """Server mode was asked for but is not safely configured. Boot stops."""


# ---------------------------------------------------------------------------
# boot-time configuration
# ---------------------------------------------------------------------------
def config_problems(password: str, secret_key: str) -> list[str]:
    """Why server mode may not boot. Messages name the VARIABLE, never its value."""
    problems: list[str] = []
    if not password:
        problems.append(
            f"{app_config.ADMIN_PASSWORD_ENV} is not set - server mode needs an "
            f"admin password of at least {MIN_PASSWORD_CHARS} characters"
        )
    elif len(password) < MIN_PASSWORD_CHARS:
        problems.append(
            f"{app_config.ADMIN_PASSWORD_ENV} is too short - use at least "
            f"{MIN_PASSWORD_CHARS} characters"
        )
    if not secret_key:
        problems.append(
            f"{app_config.SECRET_KEY_ENV} is not set - server mode signs the login "
            "cookie with it. Generate one:  python3 -c "
            "'import secrets; print(secrets.token_hex(32))'"
        )
    elif len(secret_key) < MIN_SECRET_KEY_CHARS:
        problems.append(
            f"{app_config.SECRET_KEY_ENV} is too short - use at least "
            f"{MIN_SECRET_KEY_CHARS} characters (secrets.token_hex(32) gives 64)"
        )
    if password and secret_key and hmac.compare_digest(
        password.encode("utf-8"), secret_key.encode("utf-8")
    ):
        problems.append(
            f"{app_config.ADMIN_PASSWORD_ENV} and {app_config.SECRET_KEY_ENV} must "
            "be two different values"
        )
    return problems


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def server_mode(app: Flask | None = None) -> bool:
    target = app or current_app
    return bool(target.config.get("SERVER_MODE"))


def init_app(app: Flask) -> None:
    """Call BEFORE any blueprint is registered: ``before_request`` hooks run in
    registration order and the gate has to be the first one."""
    if not server_mode(app):
        return

    # Caller override first (tests), then the environment. The password is
    # removed from app.config straight away so nothing can render or dump it.
    password = _as_text(
        app.config.pop("ADMIN_PASSWORD", None)
        or os.environ.get(app_config.ADMIN_PASSWORD_ENV, "")
    )
    secret_key = _as_text(
        app.config.get("SECRET_KEY") or os.environ.get(app_config.SECRET_KEY_ENV, "")
    ).strip()

    problems = config_problems(password, secret_key)
    if problems:
        message = (
            "REFUSING TO START in server mode (ADSPY2_SERVER_MODE is on):\n  - "
            + "\n  - ".join(problems)
        )
        log.critical(message)
        raise ServerModeConfigError(message)

    password_bytes = password.encode("utf-8")
    key_bytes = secret_key.encode("utf-8")
    app.config["SECRET_KEY"] = secret_key
    app.extensions[_EXT_KEY] = {
        "password_digest": hashlib.sha256(password_bytes).digest(),
        # What a logged-in session carries. Keyed with the secret, derived from
        # the password: changing EITHER one logs every browser out.
        "fingerprint": hmac.new(
            key_bytes, b"adspy2-admin-session:" + password_bytes, hashlib.sha256
        ).hexdigest(),
        "limiter": LoginLimiter(LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS),
    }
    del password, password_bytes

    app.config.update(
        SESSION_COOKIE_NAME="adspy2_session",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )

    # Exactly one trusted hop (the reverse proxy on this machine).
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # type: ignore[method-assign]

    app.before_request(_gate)
    app.after_request(_security_headers)
    app.register_blueprint(bp)
    log.warning(
        "SERVER MODE: login required on every screen; worker API is token-only; "
        "first-contact token hand-out is DISABLED"
    )


# ---------------------------------------------------------------------------
# rate limiter
# ---------------------------------------------------------------------------
class LoginLimiter:
    """Failed logins per client, sliding window, in memory. One gunicorn worker
    (SQLite's single writer) means one process counts every attempt."""

    MAX_CLIENTS = 10_000

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) >= self.MAX_CLIENTS:
                for other in list(self._hits):
                    self._prune(other, now)
            hits = self._hits.setdefault(key, deque())
            hits.append(now)
            self._prune(key, now)
            return len(hits)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _state() -> dict[str, Any]:
    return current_app.extensions[_EXT_KEY]


def _limiter_key(address: str | None) -> str:
    """One IPv6 customer owns a whole /64 (2^64 addresses), so counting per
    address would let them guess forever. IPv6 is bucketed by /64; IPv4 stays
    per address."""
    text = (address or "").strip()
    if not text:
        return "unknown"
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return text
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def _client_key() -> str:
    return _limiter_key(request.remote_addr)


def is_authenticated() -> bool:
    """Local mode has no login, so everyone is "in". Server mode: the session
    must carry the current fingerprint (constant-time compare)."""
    if not server_mode():
        return True
    state = current_app.extensions.get(_EXT_KEY)
    if not state:
        return False
    carried = session.get("auth")
    if not isinstance(carried, str):
        return False
    return hmac.compare_digest(carried.encode("utf-8"), state["fingerprint"].encode("utf-8"))


def verify_password(supplied: str) -> bool:
    raw = (supplied or "").encode("utf-8")
    if not raw or len(raw) > MAX_PASSWORD_BYTES:
        raw = b""
    digest = hashlib.sha256(raw).digest()
    matches = hmac.compare_digest(digest, _state()["password_digest"])
    return bool(raw) and matches


def safe_next(target: Any) -> str:
    """Open-redirect guard: ``next`` may only ever be a path on THIS site.
    ``//evil.com``, ``/\\evil.com``, ``https://evil.com`` and anything with a
    control character all collapse to ``/``."""
    text = str(target or "").strip()
    if not text.startswith("/") or text.startswith("//"):
        return "/"
    if "\\" in text or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "/"
    try:
        parts = urlsplit(text)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"
    if parts.path in ("/login", "/logout"):
        return "/"
    return text


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    # static/app.js marks every fetch() it makes; a session that expired under
    # an open tab should get a clean 401, not the login page parsed as JSON.
    if request.headers.get("X-Requested-With", "").lower() in ("fetch", "xmlhttprequest"):
        return True
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "application/json" and (
        request.accept_mimetypes[best] > request.accept_mimetypes["text/html"]
    )


def _is_worker_path(path: str) -> bool:
    return path in WORKER_PATHS or path.startswith(WORKER_PREFIXES)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def _gate():
    path = request.path or "/"
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return None

    if _is_worker_path(path):
        if request.method == "OPTIONS":
            return None
        # Token-only. Behind a proxy a loopback remote_addr proves nothing, so
        # no worker call is ever let through without the pasted token.
        from . import job_service

        supplied = str(request.headers.get(WORKER_TOKEN_HEADER) or "").strip()
        if supplied and job_service.verify_worker_token(supplied):
            return None
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "missing or invalid worker token - copy it from "
                    "the Queue screen and paste it into the extension",
                    "code": "BAD_WORKER_TOKEN",
                }
            ),
            401,
        )

    if is_authenticated():
        return None

    if _wants_json():
        return (
            jsonify({"ok": False, "error": "login required", "code": "LOGIN_REQUIRED"}),
            401,
        )
    target = request.full_path if request.method in ("GET", "HEAD") else "/"
    target = safe_next(target.rstrip("?"))
    if target == "/":
        return redirect("/login", code=303)
    from urllib.parse import quote

    return redirect("/login?next=" + quote(target, safe="/"), code=303)


def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path in ("/login", "/logout"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# /login, /logout  (registered in server mode only)
# ---------------------------------------------------------------------------
def _render_login(next_target: str, error: str = "", status: int = 200):
    return (
        render_template("login.html", next_target=next_target, error=error),
        status,
    )


@bp.get("/login")
def login_form():
    target = safe_next(request.args.get("next"))
    if is_authenticated():
        return redirect(target, code=303)
    return _render_login(target)


@bp.post("/login")
def login_submit():
    target = safe_next(request.form.get("next") or request.args.get("next"))
    limiter: LoginLimiter = _state()["limiter"]
    client = _client_key()

    if limiter.blocked(client):
        log.warning("login refused (rate limit) for %s", client)
        return _render_login(
            target, "Too many attempts. Wait a few minutes and try again.", 429
        )

    if not verify_password(request.form.get("password", "")):
        failures = limiter.record_failure(client)
        # The attempt is logged; what was typed never is.
        log.warning("failed login from %s (%s in window)", client, failures)
        return _render_login(target, "Login failed.", 401)

    limiter.reset(client)
    session.clear()                      # new session: nothing pre-login survives
    session.permanent = True
    session["auth"] = _state()["fingerprint"]
    session["sid"] = secrets.token_hex(8)
    log.info("login ok from %s", client)
    return redirect(target, code=303)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect("/login", code=303)


# ---------------------------------------------------------------------------
# `python -m app.auth` — the launcher's pre-flight, so a bad server-mode
# environment fails in the terminal instead of inside a daemonised gunicorn.
# ---------------------------------------------------------------------------
def environment_problems() -> list[str]:
    if not app_config.server_mode_from_env():
        return []
    return config_problems(
        os.environ.get(app_config.ADMIN_PASSWORD_ENV, ""),
        os.environ.get(app_config.SECRET_KEY_ENV, "").strip(),
    )


if __name__ == "__main__":                                # pragma: no cover
    found = environment_problems()
    for line in found:
        print(line, file=sys.stderr)
    sys.exit(1 if found else 0)
