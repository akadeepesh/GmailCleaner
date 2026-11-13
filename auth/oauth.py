"""
GmailCleaner — Google OAuth 2.0
Handles the full authorization lifecycle:
  - First-run browser-based consent flow
  - Token persistence to disk (local dev) or the GOOGLE_TOKEN_JSON env var
    injected from Secret Manager (production)
  - Silent refresh on subsequent runs
  - Scope validation

Gmail API scopes requested:
  - gmail.readonly  : fetch and read emails (scan)
  - gmail.modify    : move to Trash, label (execute)
  - gmail.send      : send the daily digest and mailto unsubscribes
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from core.fsutil import atomic_write_text
from core.paths import app_home, is_headless, resource_path

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# OAuth scopes
# ──────────────────────────────────────────────────────────────────────────────

SCOPES = [
    # Read-only access to mailbox — used during scans.
    "https://www.googleapis.com/auth/gmail.readonly",
    # Modify labels and move to Trash — used when executing confirmed actions.
    "https://www.googleapis.com/auth/gmail.modify",
    # Send the daily digest email back to the user's own inbox.
    "https://www.googleapis.com/auth/gmail.send",
]


REVOKE_URL = "https://oauth2.googleapis.com/revoke"
BUNDLED_CLIENT = "bundled/oauth_client.json"


class SignInRequired(RuntimeError):
    """No usable token, and an interactive consent flow is not allowed here."""


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def load_credentials(cfg: dict, interactive: bool = True) -> Credentials:
    """
    Return valid credentials using the paths resolved in the runtime config.

    On Cloud Run the token is injected from Secret Manager as the
    GOOGLE_TOKEN_JSON environment variable; locally it is read from disk.
    With interactive=False (the dashboard), a missing or revoked token raises
    SignInRequired instead of opening a consent flow.
    """
    auth_cfg = cfg.get("auth", {})
    args = (
        auth_cfg.get("credentials_path", "config/credentials.json"),
        auth_cfg.get("token_path", "config/token.json"),
    )
    if interactive:
        return get_credentials(*args)
    return get_credentials(*args, interactive=False)


def get_credentials(
    credentials_path: str,
    token_path: str,
    interactive: bool = True,
) -> Credentials:
    """
    Return valid Google OAuth credentials, handling the full lifecycle:

    0. If GOOGLE_TOKEN_JSON is set (production) → load it, refresh in memory.
    1. If a saved token exists and is valid → return it immediately.
    2. If the saved token is expired but has a refresh token → refresh silently.
    3. If no token exists or refresh fails → launch browser consent flow
       (never in headless mode — see GMAILCLEANER_HEADLESS).
    4. Persist the resulting token for future runs.

    Args:
        credentials_path: Path to the OAuth client credentials JSON file
                          downloaded from Google Cloud Console.
        token_path:       Path where the user token will be read from / saved to.

    Returns:
        A valid, unexpired google.oauth2.credentials.Credentials object.

    Raises:
        FileNotFoundError: If credentials_path does not exist and a consent flow is needed.
        RuntimeError:      If the OAuth flow fails, or a headless run has no usable token.
        SignInRequired:    With interactive=False, when there is no usable token.
    """
    token_json = os.getenv("GOOGLE_TOKEN_JSON", "").strip()
    if token_json:
        return _credentials_from_env(token_json)

    creds = _load_token(token_path)

    if creds and creds.valid:
        logger.debug("Loaded valid token from %s", token_path)
        _assert_scopes(creds)
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds = _refresh_token(creds, token_path)
        if creds:
            return creds

    if not interactive:
        raise SignInRequired("Please sign in again: Google sign-in has expired or was removed.")

    if _is_headless():
        raise RuntimeError(
            f"No valid OAuth token at {token_path} and running headless. "
            "Generate a token locally with `python main.py info`, then provide it "
            "via GOOGLE_TOKEN_JSON or GOOGLE_TOKEN_PATH."
        )

    creds_file = Path(credentials_path)
    if not creds_file.exists():
        raise FileNotFoundError(
            f"OAuth credentials not found at: {credentials_path}\n"
            "Download your credentials from Google Cloud Console → APIs & Services → Credentials\n"
            "and save them to config/credentials.json (or update GOOGLE_CREDENTIALS_PATH in .env)."
        )

    # No valid token — run the browser consent flow.
    logger.info("No valid token found. Launching OAuth consent flow in browser...")
    creds = _run_consent_flow(credentials_path, token_path)
    return creds


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _credentials_from_env(token_json: str) -> Credentials:
    """
    Production path: build credentials from the GOOGLE_TOKEN_JSON env var.

    The token JSON written by the local consent flow already contains the
    client_id, client_secret and refresh_token, so no credentials file is
    needed. Refreshed access tokens are kept in memory only.
    """
    try:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"GOOGLE_TOKEN_JSON is not a valid authorized-user token: {exc}") from exc

    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError("GOOGLE_TOKEN_JSON has no refresh_token; re-run the consent flow locally.")
        refreshed = _refresh_token(creds, token_path=None)
        if not refreshed:
            raise RuntimeError(
                "Token from GOOGLE_TOKEN_JSON could not be refreshed. It may have been revoked, "
                "or the OAuth consent screen is still in 'Testing' mode (tokens expire after 7 days). "
                "Re-run `python main.py info` locally and update the token secret."
            )
        creds = refreshed

    _assert_scopes(creds)
    logger.debug("Loaded credentials from GOOGLE_TOKEN_JSON")
    return creds


def _is_headless() -> bool:
    """True when running in a container / CI where a browser flow is impossible."""
    return is_headless()


def _load_token(token_path: str) -> Optional[Credentials]:
    """Load a previously saved token. Returns None if the file does not exist."""
    path = Path(token_path)
    if not path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        logger.debug("Token file loaded from %s", token_path)
        return creds
    except Exception as exc:
        logger.warning("Failed to parse token file at %s: %s. Will re-authenticate.", token_path, exc)
        return None


def _refresh_token(creds: Credentials, token_path: Optional[str]) -> Optional[Credentials]:
    """Attempt a silent token refresh. Saves the new token if token_path is provided."""
    try:
        creds.refresh(Request())
        logger.info("OAuth token refreshed silently.")
        if token_path:
            _save_token(creds, token_path)
        return creds
    except RefreshError as exc:
        logger.warning("Token refresh failed (%s). Will re-run consent flow.", exc)
        return None


def _run_consent_flow(credentials_path: str, token_path: str) -> Credentials:
    """
    Launch the browser-based OAuth consent flow (InstalledAppFlow).

    Opens a local HTTP server on a random port. The user authenticates
    in their browser and the authorization code is captured automatically.
    On headless environments, prints the URL for manual completion.
    """
    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)

    try:
        # run_local_server opens the browser and captures the redirect automatically.
        creds = flow.run_local_server(
            port=0,           # Use a random available port.
            prompt="consent", # Force re-consent to ensure refresh_token is included.
            access_type="offline",
        )
    except Exception as exc:
        raise RuntimeError(
            f"OAuth consent flow failed: {exc}\n"
            "If running headlessly, generate the token on a machine with a browser "
            "and supply it via GOOGLE_TOKEN_JSON."
        ) from exc

    _save_token(creds, token_path)
    logger.info("OAuth consent completed. Token saved to %s", token_path)
    return creds


def _save_token(creds: Credentials, token_path: str) -> None:
    """Persist credentials to disk as JSON, atomically, readable only by the current user."""
    atomic_write_text(token_path, creds.to_json(), mode=0o600)
    logger.debug("Token saved to %s (mode 600)", token_path)


def save_token(creds: Credentials, token_path: str) -> None:
    """Public wrapper used by the dashboard's sign-in flow."""
    _save_token(creds, token_path)


def token_status(token_path: str) -> str:
    """
    "signed_in" if a token file holds a usable or refreshable token, else
    "needs_sign_in". Never refreshes and never touches the network.
    """
    creds = _load_token(token_path)
    if creds and (creds.valid or creds.refresh_token):
        return "signed_in"
    return "needs_sign_in"


def revoke_token(token_path: str, timeout: float = 10.0) -> bool:
    """
    Revoke a saved token at Google (best effort). Returns True if Google
    confirmed it. The caller deletes the token file either way.
    """
    try:
        info = json.loads(Path(token_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    token = info.get("refresh_token") or info.get("token")
    if not token:
        return False
    try:
        resp = httpx.post(
            REVOKE_URL,
            data={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
        return resp.status_code == 200
    except httpx.HTTPError as exc:
        logger.warning("Token revocation failed: %s", type(exc).__name__)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# OAuth client (credentials.json) lookup
# ──────────────────────────────────────────────────────────────────────────────

def client_config_candidates() -> list[Path]:
    """
    Where to look for the OAuth client, first match wins:
      1. GOOGLE_CREDENTIALS_PATH env
      2. <home>/credentials.json (uploaded by the user in Settings)
      3. config/credentials.json (developer checkout)
      4. the client bundled into the desktop build at release time
    """
    candidates: list[Path] = []
    env = os.getenv("GOOGLE_CREDENTIALS_PATH", "").strip()
    if env:
        candidates.append(Path(env))
    home = app_home()
    if home is not None:
        candidates.append(home / "credentials.json")
    candidates.append(Path("config") / "credentials.json")
    candidates.append(resource_path(BUNDLED_CLIENT))
    return candidates


def resolve_client_config() -> Optional[dict]:
    """Return the first valid OAuth client config dict, or None if there is none."""
    for path in client_config_candidates():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable OAuth client file at %s", path)
            continue
        if validate_client_config(data, allow_web=True) is None:
            return data
        logger.warning("Ignoring invalid OAuth client file at %s", path)
    return None


def user_client_path() -> Path:
    home = app_home()
    return home / "credentials.json" if home is not None else Path("config") / "credentials.json"


def validate_client_config(data, allow_web: bool = False) -> Optional[str]:
    """Return None if data is a usable OAuth client JSON, else a plain-language error."""
    if not isinstance(data, dict):
        return "That file is not a Google sign-in client file."
    kinds = ("installed", "web") if allow_web else ("installed",)
    section = next((data[k] for k in kinds if isinstance(data.get(k), dict)), None)
    if section is None:
        return ("This is not a Desktop app client. In Google Cloud Console, create an "
                "OAuth client ID of type Desktop app and download its JSON file.")
    for field in ("client_id", "auth_uri", "token_uri"):
        if not isinstance(section.get(field), str) or not section[field]:
            return f"The client file is missing {field}."
    if not section["client_id"].endswith(".apps.googleusercontent.com"):
        return "The client_id does not look like a Google OAuth client ID."
    return None


def save_user_client_config(data: dict) -> Path:
    """Validate and store an uploaded Desktop-app client JSON (mode 0600)."""
    error = validate_client_config(data)
    if error:
        raise ValueError(error)
    return atomic_write_text(user_client_path(), json.dumps(data, indent=2), mode=0o600)


def _assert_scopes(creds: Credentials) -> None:
    """
    Warn if the saved token was granted narrower scopes than currently required.
    This happens when SCOPES is expanded after the first run.
    """
    if not creds.scopes:
        return
    missing = set(SCOPES) - set(creds.scopes)
    if missing:
        logger.warning(
            "Saved token is missing scopes: %s. "
            "Delete config/token.json and re-run to re-authenticate with full scopes.",
            missing,
        )