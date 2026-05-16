"""
GmailCleaner — Google OAuth 2.0
Handles the full authorization lifecycle:
  - First-run browser-based consent flow
  - Token persistence to disk (local dev) or Secret Manager (production)
  - Silent refresh on subsequent runs
  - Scope validation

Gmail API scopes requested:
  - gmail.readonly  : fetch and read emails (scan phase)
  - gmail.modify    : delete, label, send (execute phase)
  - gmail.send      : send the daily digest
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
    # Read-only access to mailbox — used during scan phase.
    "https://www.googleapis.com/auth/gmail.readonly",
    # Modify labels, move to trash, batch delete — used during execute phase.
    "https://www.googleapis.com/auth/gmail.modify",
    # Send the daily digest email back to the user's own inbox.
    "https://www.googleapis.com/auth/gmail.send",
]


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def get_credentials(
    credentials_path: str,
    token_path: str,
) -> Credentials:
    """
    Return valid Google OAuth credentials, handling the full lifecycle:

    1. If a saved token exists and is valid → return it immediately.
    2. If the saved token is expired but has a refresh token → refresh silently.
    3. If no token exists or refresh fails → launch browser consent flow.
    4. Persist the resulting token for future runs.

    Args:
        credentials_path: Path to the OAuth client credentials JSON file
                          downloaded from Google Cloud Console.
        token_path:       Path where the user token will be read from / saved to.

    Returns:
        A valid, unexpired google.oauth2.credentials.Credentials object.

    Raises:
        FileNotFoundError: If credentials_path does not exist.
        RuntimeError:      If the OAuth flow fails or scopes are insufficient.
    """
    creds_file = Path(credentials_path)
    if not creds_file.exists():
        raise FileNotFoundError(
            f"OAuth credentials not found at: {credentials_path}\n"
            "Download your credentials from Google Cloud Console → APIs & Services → Credentials\n"
            "and save them to config/credentials.json (or update GOOGLE_CREDENTIALS_PATH in .env)."
        )

    creds = _load_token(token_path)

    if creds and creds.valid:
        logger.debug("Loaded valid token from %s", token_path)
        _assert_scopes(creds)
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds = _refresh_token(creds, token_path)
        if creds:
            return creds

    # No valid token — run the browser consent flow.
    logger.info("No valid token found. Launching OAuth consent flow in browser...")
    creds = _run_consent_flow(credentials_path, token_path)
    return creds


def get_credentials_from_secret_manager(
    project_id: str,
    credentials_secret: str = "gmail-cleaner-credentials",
    token_secret: str = "gmail-cleaner-token",
) -> Credentials:
    """
    Production path: load credentials and token from Google Secret Manager.

    This function is used when deployed to Cloud Run, where no local
    filesystem is available for credential storage.

    Args:
        project_id:          GCP project ID.
        credentials_secret:  Secret Manager secret name for the OAuth credentials JSON.
        token_secret:        Secret Manager secret name for the saved token JSON.

    Returns:
        Valid Google OAuth credentials.
    """
    try:
        from google.cloud import secretmanager  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "google-cloud-secret-manager is required for production credential loading. "
            "Install it with: pip install google-cloud-secret-manager"
        ) from exc

    client = secretmanager.SecretManagerServiceClient()

    def _access(secret_name: str) -> dict:
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return json.loads(response.payload.data.decode("utf-8"))

    credentials_data = _access(credentials_secret)
    token_data = _access(token_secret)

    creds = Credentials.from_authorized_user_info(token_data, SCOPES)

    if creds.expired and creds.refresh_token:
        creds = _refresh_token(creds, token_path=None)

    if not creds or not creds.valid:
        raise RuntimeError(
            "Credentials loaded from Secret Manager are invalid or expired. "
            "Re-run the OAuth consent flow locally and update the token secret."
        )

    return creds


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

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
            "If running headlessly, ensure a browser is available or use the "
            "Secret Manager path for production deployments."
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