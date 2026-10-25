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

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

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


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def load_credentials(cfg: dict) -> Credentials:
    """
    Return valid credentials using the paths resolved in the runtime config.

    On Cloud Run the token is injected from Secret Manager as the
    GOOGLE_TOKEN_JSON environment variable; locally it is read from disk.
    """
    auth_cfg = cfg.get("auth", {})
    return get_credentials(
        auth_cfg.get("credentials_path", "config/credentials.json"),
        auth_cfg.get("token_path", "config/token.json"),
    )


def get_credentials(
    credentials_path: str,
    token_path: str,
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
    flag = os.getenv("GMAILCLEANER_HEADLESS", "").lower()
    if flag in ("1", "true", "yes"):
        return True
    if flag in ("0", "false", "no"):
        return False
    # Cloud Run sets K_SERVICE (services) or CLOUD_RUN_JOB (jobs).
    return bool(os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB"))


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
    """Persist credentials to disk as JSON. Creates parent directories if needed."""
    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    # Restrict file permissions: readable only by the current user.
    os.chmod(path, 0o600)
    logger.debug("Token saved to %s (mode 600)", token_path)


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