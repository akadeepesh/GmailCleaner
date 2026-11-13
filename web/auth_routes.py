"""
GmailCleaner — Google sign-in and account management in the dashboard.

Routes:
  GET  /accounts                — list accounts, add / switch / sign out / remove data
  GET  /auth/start[?account=]   — redirect to Google's consent screen (PKCE + state)
  GET  /auth/callback           — finish sign-in, save the token, pick the account
  POST /api/accounts/signout    — revoke at Google and delete the saved token
  POST /api/accounts/remove     — sign out and delete that account's local data

Sign-in runs through the dashboard itself (no terminal, no second local
server): the redirect URI is http://<loopback host:port>/auth/callback, which
Desktop-app OAuth clients accept on any port. Everything here is disabled
(404) when headless or on Cloud Run, which must never expose a sign-in flow.
"""

import gc
import hmac
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from flask import Flask, abort, make_response, redirect, render_template, request, session

from auth.oauth import SCOPES, resolve_client_config, revoke_token, save_token
from core.paths import is_headless
from gmail.client import GmailClient
from web.accounts import AccountInfo, account_name_for_email, key_for
from web.common import (
    ACCOUNT_COOKIE,
    LOOPBACK_HOSTS,
    current_account,
    is_desktop,
    jobs,
    json_error,
    registry,
    require_token,
)

logger = logging.getLogger(__name__)

_SESSION_KEY = "gc_oauth"


def sign_in_enabled() -> bool:
    return not is_headless() and not os.getenv("K_SERVICE")


def _loopback_request() -> bool:
    host = request.host or ""
    hostname = host.rpartition(":")[0] or host
    return hostname in LOOPBACK_HOSTS


def build_flow(client_config: dict, redirect_uri: str, state: Optional[str] = None,
               code_verifier: Optional[str] = None):
    """Build the google_auth_oauthlib Flow (separate function so tests can mock it)."""
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
        autogenerate_code_verifier=True,
    )


def fetch_profile_email(creds) -> str:
    return GmailClient(creds).get_profile().get("emailAddress", "")


def register_auth_routes(app: Flask) -> None:

    # ── Accounts page ─────────────────────────────────────────────────────────

    @app.get("/accounts")
    @require_token
    def accounts_page():
        reg = registry()
        rows = []
        for info in reg.list_accounts():
            try:
                last_run = reg.get(info.name).db.get_dashboard_summary()["last_run"]
            except Exception:
                last_run = None
            rows.append({"info": info, "last_run": last_run})
        return render_template(
            "accounts.html",
            active="accounts",
            rows=rows,
            current_key=key_for(current_account()),
            sign_in_enabled=sign_in_enabled(),
            has_client=resolve_client_config() is not None,
        )

    # ── Sign-in ───────────────────────────────────────────────────────────────

    @app.get("/auth/start")
    @require_token
    def auth_start():
        if not sign_in_enabled() or not _loopback_request():
            abort(404)
        client_config = resolve_client_config()
        if client_config is None:
            return render_template("auth_error.html", reason="no_client", active="accounts")

        redirect_uri = f"http://{request.host}/auth/callback"
        flow = build_flow(client_config, redirect_uri)
        auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
        session[_SESSION_KEY] = {
            "state": state,
            "verifier": flow.code_verifier,
            "redirect_uri": redirect_uri,
        }
        return redirect(auth_url)

    @app.get("/auth/callback")
    def auth_callback():
        """
        Not token-protected: the browser arrives here from Google, so the
        SameSite=Strict gc_token cookie is not sent. The one-time state stored
        in the session at /auth/start (which is token-protected) proves the
        flow was started from this dashboard.
        """
        if not sign_in_enabled() or not _loopback_request():
            abort(404)
        pending = session.pop(_SESSION_KEY, None)
        state = request.args.get("state", "")
        if not pending or not state or not hmac.compare_digest(state, pending.get("state", "")):
            return _auth_error("state", 400)
        if request.args.get("error"):
            return _auth_error("denied", 400)
        code = request.args.get("code", "")
        client_config = resolve_client_config()
        if not code or client_config is None:
            return _auth_error("exchange", 400)

        flow = build_flow(client_config, pending["redirect_uri"], state=state,
                          code_verifier=pending.get("verifier"))
        relax = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"  # we check scopes ourselves below
        try:
            flow.fetch_token(code=code)
            creds = flow.credentials
        except Exception as exc:
            logger.warning("Sign-in token exchange failed: %s", type(exc).__name__)
            return _auth_error("exchange", 400)
        finally:
            if relax is None:
                os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
            else:
                os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = relax

        granted = getattr(creds, "granted_scopes", None)
        if granted and set(SCOPES) - set(granted):
            return _auth_error("scopes", 400)

        try:
            email = fetch_profile_email(creds)
            reg = registry()
            name = reg.pinned or account_name_for_email(email)
        except Exception as exc:
            logger.warning("Could not read the Gmail profile after sign-in: %s", type(exc).__name__)
            return _auth_error("profile", 400)

        first = not any(a.signed_in for a in reg.list_accounts())
        save_token(creds, reg.resolve(name)["auth"]["token_path"])
        reg.remember_email(name, email)
        reg.evict(name)
        logger.info("Signed in account %s", name)

        target = "/?welcome=1" if first else "/accounts"
        resp = make_response(render_template("auth_done.html", target=target, email=email))
        from web.app import set_account_cookie

        set_account_cookie(resp, key_for(name))
        return resp

    # ── Sign out / remove data ────────────────────────────────────────────────

    @app.post("/api/accounts/signout")
    @require_token
    def api_signout():
        info, error = _account_from_body()
        if error:
            return error
        _sign_out(info)
        resp = make_response({"status": "ok"})
        if request.cookies.get(ACCOUNT_COOKIE) == info.key:
            resp.delete_cookie(ACCOUNT_COOKIE)
        return resp

    @app.post("/api/accounts/remove")
    @require_token
    def api_remove():
        info, error = _account_from_body()
        if error:
            return error
        if (request.get_json(silent=True) or {}).get("confirm") != "REMOVE":
            return json_error("Please confirm removing this account's data", 400)
        if jobs().active(info.name):
            return json_error("A scan is running for this account. Try again when it finishes.", 409)
        _sign_out(info)
        try:
            _remove_data(info)
        except OSError as exc:
            logger.warning("Could not remove data for %s: %s", info.key, exc)
            return json_error("Some files are in use. Close other GmailCleaner windows and try again.", 409)
        resp = make_response({"status": "ok"})
        if request.cookies.get(ACCOUNT_COOKIE) == info.key:
            resp.delete_cookie(ACCOUNT_COOKIE)
        return resp


def _account_from_body():
    if not sign_in_enabled():
        abort(404)
    key = (request.get_json(silent=True) or {}).get("account")
    info = registry().find(key) if isinstance(key, str) else None
    if info is None:
        return None, json_error("Unknown account", 404)
    return info, None


def _sign_out(info: AccountInfo) -> None:
    reg = registry()
    if Path(info.token_path).is_file():
        if not revoke_token(info.token_path):
            logger.info("Google did not confirm token revocation for %s; deleting it locally.", info.key)
        Path(info.token_path).unlink(missing_ok=True)
    reg.forget_email(info.name)
    reg.evict(info.name)


def _remove_data(info: AccountInfo) -> None:
    """Delete the account's database, report and data folder."""
    reg = registry()
    cfg = reg.resolve(info.name)
    reg.evict(info.name)
    gc.collect()  # release lingering SQLite handles (Windows refuses to delete open files)

    db_path = Path(cfg["database"]["path"])
    report_path = Path(cfg["output"]["pending_actions_path"])
    if info.name:
        # Named accounts own their folders: data/<name>/ and output/<name>/.
        for folder in (db_path.parent, report_path.parent):
            if folder.name == info.name and folder.is_dir():
                shutil.rmtree(folder)
    else:
        # The legacy account shares data/ with the named accounts' folders.
        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"), report_path):
            path.unlink(missing_ok=True)


def _auth_error(reason: str, status: int):
    return make_response(render_template("auth_error.html", reason=reason, active="accounts"), status)


__all__ = ["register_auth_routes", "sign_in_enabled", "build_flow", "fetch_profile_email", "is_desktop"]
