"""
GmailCleaner — helpers shared by the dashboard's route modules.

  * require_token: the DASHBOARD_SECRET check (Bearer header, ?token=, cookie)
  * install_security: desktop-mode hardening (Host allowlist, CSRF header)
  * current_ctx / current_account: the account chosen for this request
"""

import hmac
import os
from functools import wraps
from typing import Optional

from flask import Flask, abort, current_app, g, jsonify, make_response, render_template, request

from core.jobs import JobRunner
from web.accounts import LEGACY_KEY, AccountContext, AccountRegistry, name_for

TOKEN_COOKIE = "gc_token"
ACCOUNT_COOKIE = "gc_account"
CSRF_HEADER = "X-GC-Request"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


# ──────────────────────────────────────────────────────────────────────────────
# App services
# ──────────────────────────────────────────────────────────────────────────────

def registry() -> AccountRegistry:
    return current_app.config["ACCOUNTS"]


def jobs() -> JobRunner:
    return current_app.config["JOBS"]


def is_desktop() -> bool:
    return bool(current_app.config.get("DESKTOP"))


def json_error(message: str, status: int, **extra):
    return jsonify({"error": message, **extra}), status


# ──────────────────────────────────────────────────────────────────────────────
# Account for this request
# ──────────────────────────────────────────────────────────────────────────────

def current_account() -> Optional[str]:
    """
    ?account= → gc_account cookie → (desktop) first signed-in account → None.

    Only known accounts are accepted, so a crafted value can never create
    state for an arbitrary name. Pinned (GMAILCLEANER_ACCOUNT) always wins.
    """
    if "gc_account" in g:
        return g.gc_account
    reg = registry()
    if reg.pinned:
        g.gc_account = reg.pinned
        return g.gc_account

    accounts = reg.list_accounts()
    known = {a.key for a in accounts}
    chosen: Optional[str] = None
    for requested in (request.args.get("account"), request.cookies.get(ACCOUNT_COOKIE)):
        if requested and requested in known:
            chosen = requested
            break
    if chosen is None and is_desktop():
        signed_in = [a for a in accounts if a.signed_in]
        if signed_in:
            chosen = signed_in[0].key
    g.gc_account = name_for(chosen)
    return g.gc_account


def current_ctx() -> AccountContext:
    if "gc_ctx" not in g:
        g.gc_ctx = registry().get(current_account())
    return g.gc_ctx


# ──────────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────────

def safe_equals(candidate: str, secret: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), secret.encode())


def is_authenticated() -> bool:
    secret = os.getenv("DASHBOARD_SECRET", "")
    if not secret:
        return True
    auth_header = request.headers.get("Authorization", "")
    header_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    query_token = request.args.get("token", "")
    cookie_token = request.cookies.get(TOKEN_COOKIE, "")
    return any(safe_equals(t, secret) for t in (header_token, query_token, cookie_token))


def require_token(f):
    """
    Decorator that checks for a valid dashboard secret token.

    Accepts the token as:
      - Authorization: Bearer <token>
      - ?token=<token> query parameter (also sets the session cookie)
      - the gc_token cookie

    If DASHBOARD_SECRET is not set, authentication is disabled
    (suitable for local development only).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = os.getenv("DASHBOARD_SECRET", "")
        if not secret:
            return f(*args, **kwargs)  # Auth disabled

        if not is_authenticated():
            if is_desktop() and request.method == "GET" and not request.path.startswith("/api/"):
                # A bookmark after a restart: the per-launch secret has changed.
                return make_response(render_template("locked.html"), 401)
            abort(401)

        response = make_response(f(*args, **kwargs))
        query_token = request.args.get("token", "")
        if query_token and safe_equals(query_token, secret):
            response.set_cookie(
                TOKEN_COOKIE,
                secret,
                httponly=True,
                samesite="Strict",
                secure=request.is_secure
                or request.headers.get("X-Forwarded-Proto", "") == "https",
                max_age=30 * 24 * 3600,
            )
        return response
    return decorated


# ──────────────────────────────────────────────────────────────────────────────
# Desktop-mode hardening (plan D13)
# ──────────────────────────────────────────────────────────────────────────────

def install_security(app: Flask) -> None:
    """
    In desktop mode, before every request:
      * reject a Host header other than 127.0.0.1 / localhost (DNS rebinding)
      * require X-GC-Request: 1 and a JSON body on POST /api/* and /auth/*
        (CSRF: a cross-site form or fetch can't set both)
    The Pub/Sub webhook is exempt; it is unused on desktop.
    """

    @app.before_request
    def _desktop_guards():
        if not app.config.get("DESKTOP"):
            return None
        if not host_allowed(request.host, app.config.get("GC_PORT")):
            return json_error("Unknown host", 400)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and (
            request.path.startswith("/api/") or request.path.startswith("/auth/")
        ):
            if request.headers.get(CSRF_HEADER) != "1" or not request.is_json:
                return json_error("Missing request header", 403)
        return None


def host_allowed(host: str, port: Optional[int] = None) -> bool:
    hostname, _, host_port = (host or "").rpartition(":")
    if not hostname:  # no port in the Host header
        hostname, host_port = host_port, ""
    if hostname not in LOOPBACK_HOSTS:
        return False
    if port and host_port != str(port):
        return False
    return True


__all__ = [
    "ACCOUNT_COOKIE", "LEGACY_KEY", "TOKEN_COOKIE", "current_account", "current_ctx",
    "install_security", "is_desktop", "jobs", "json_error", "registry", "require_token",
]
