"""
GmailCleaner — Daily Digest Engine
Builds a structured HTML email summarising the scan results and sends it
to the authenticated user's own inbox via the Gmail API.

The digest contains:
  - Run metadata (timestamp, emails scanned)
  - NEEDS_REPLY items (up to 5, with sender, subject, snippet)
  - FYI count
  - PROMOTIONS count + top unsubscribe candidates
  - JUNK count + top cleanup groups
  - UNCLASSIFIED items flagged for review
  - A link to the dashboard (if DASHBOARD_URL is set) and the CLI command
    to execute confirmed actions

Sending to self (same Gmail account) requires only the gmail.send scope —
no external mail infrastructure needed.
"""

import base64
import html as html_lib
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from engines.triage import TriageSummary, TriageResult
from engines.unsubscribe import UnsubscribeCandidate
from engines.bulk_cleanup import CleanupGroup
from gmail.client import GmailClient

logger = logging.getLogger(__name__)

# Max items shown per section in the digest.
MAX_NEEDS_REPLY = 5
MAX_UNSUB_CANDIDATES = 5
MAX_CLEANUP_GROUPS = 5
MAX_UNCLASSIFIED = 5


# ── Public API ────────────────────────────────────────────────────────────────

class DigestEngine:
    """
    Builds and sends the daily digest email.

    Usage:
        engine = DigestEngine(client, recipient="you@gmail.com")
        engine.send(run_id, emails_scanned, triage_summary,
                    unsubscribe_candidates, cleanup_groups)
    """

    def __init__(
        self,
        client: GmailClient,
        recipient: str,
        dashboard_url: Optional[str] = None,
        session_path: str = "output/pending_actions.json",
    ) -> None:
        self._client = client
        self._recipient = recipient
        self._dashboard_url = dashboard_url
        self._session_path = session_path

    def send(
        self,
        run_id: str,
        emails_scanned: int,
        triage: TriageSummary,
        unsubscribe_candidates: list[UnsubscribeCandidate],
        cleanup_groups: list[CleanupGroup],
    ) -> bool:
        """
        Build and send the digest email.

        Args:
            run_id:                  Current scan run ID.
            emails_scanned:          Total emails fetched in this scan.
            triage:                  TriageSummary from the triage engine.
            unsubscribe_candidates:  New unsubscribe candidates.
            cleanup_groups:          Detected cleanup groups.

        Returns:
            True if the email was sent successfully, False otherwise.
        """
        if not self._recipient:
            logger.warning("Digest: no recipient configured — skipping send.")
            return False

        subject = _build_subject(triage)
        html = _build_html(
            run_id=run_id,
            emails_scanned=emails_scanned,
            triage=triage,
            unsubscribe_candidates=unsubscribe_candidates,
            cleanup_groups=cleanup_groups,
            dashboard_url=self._dashboard_url,
            session_path=self._session_path,
        )

        try:
            raw = _compose_raw(
                to=self._recipient,
                subject=subject,
                html=html,
            )
            result = self._client.send_message(raw)
            logger.info(
                "Digest sent to %s (message_id=%s)",
                self._recipient, result.get("id"),
            )
            return True
        except Exception as exc:
            logger.error("Failed to send digest: %s", exc)
            return False


# ── Subject line ──────────────────────────────────────────────────────────────

def _build_subject(triage: TriageSummary) -> str:
    today = datetime.now(timezone.utc).strftime("%b %d")
    needs_reply = len(triage.needs_reply)
    if needs_reply > 0:
        return f"📬 GmailCleaner · {today} — {needs_reply} need{'s' if needs_reply == 1 else ''} reply"
    return f"📬 GmailCleaner · {today} — Inbox digest"


# ── HTML builder ──────────────────────────────────────────────────────────────

def _build_html(
    run_id: str,
    emails_scanned: int,
    triage: TriageSummary,
    unsubscribe_candidates: list[UnsubscribeCandidate],
    cleanup_groups: list[CleanupGroup],
    dashboard_url: Optional[str] = None,
    session_path: str = "output/pending_actions.json",
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────────
    sections.append(_header(now, emails_scanned, triage))

    # ── NEEDS_REPLY ───────────────────────────────────────────────────────────
    if triage.needs_reply:
        sections.append(_section_needs_reply(triage.needs_reply))

    # ── FYI ───────────────────────────────────────────────────────────────────
    if triage.fyi:
        sections.append(_section_fyi(triage.fyi))

    # ── PROMOTIONS + unsubscribe candidates ───────────────────────────────────
    if triage.promotions or unsubscribe_candidates:
        sections.append(_section_promotions(triage.promotions, unsubscribe_candidates))

    # ── JUNK + cleanup groups ─────────────────────────────────────────────────
    if triage.junk or cleanup_groups:
        sections.append(_section_junk(triage.junk, cleanup_groups))

    # ── UNCLASSIFIED ──────────────────────────────────────────────────────────
    if triage.unclassified:
        sections.append(_section_unclassified(triage.unclassified))

    # ── Action footer ─────────────────────────────────────────────────────────
    has_actions = bool(unsubscribe_candidates or cleanup_groups)
    sections.append(_footer(run_id, has_actions, dashboard_url, session_path))

    return _wrap_html("\n".join(sections))


# ── Section builders ──────────────────────────────────────────────────────────

def _header(timestamp: str, emails_scanned: int, triage: TriageSummary) -> str:
    counts = [
        ("📨", "Need Reply", len(triage.needs_reply), "#d32f2f"),
        ("📄", "FYI", len(triage.fyi), "#1565c0"),
        ("🏷", "Promotions", len(triage.promotions), "#e65100"),
        ("🗑", "Junk", len(triage.junk), "#4e342e"),
    ]
    pills = " ".join(
        f'<span style="background:{color};color:#fff;padding:3px 10px;'
        f'border-radius:12px;font-size:13px;margin:2px 4px 2px 0;display:inline-block;">'
        f'{icon} {label}: {count}</span>'
        for icon, label, count, color in counts
        if count > 0
    )
    return f"""
<div style="background:#1a237e;color:#fff;padding:28px 32px 20px;border-radius:10px 10px 0 0;">
  <h1 style="margin:0 0 6px;font-size:22px;font-weight:700;letter-spacing:-0.5px;">
    📬 GmailCleaner Daily Digest
  </h1>
  <p style="margin:0;font-size:13px;opacity:0.75;">{timestamp} &nbsp;·&nbsp; {emails_scanned:,} emails scanned</p>
  <div style="margin-top:16px;">{pills}</div>
</div>
"""


def _section_needs_reply(items: list[TriageResult]) -> str:
    rows = ""
    for item in items[:MAX_NEEDS_REPLY]:
        unread_dot = (
            '<span style="width:8px;height:8px;border-radius:50%;'
            'background:#d32f2f;display:inline-block;margin-right:6px;"></span>'
            if item.is_unread else ""
        )
        rows += f"""
<tr>
  <td style="padding:10px 0;border-bottom:1px solid #f0f0f0;vertical-align:top;width:160px;">
    <span style="font-weight:600;font-size:13px;color:#333;">{_esc(item.sender_name or item.sender_email)}</span><br>
    <span style="font-size:11px;color:#888;">{item.date}</span>
  </td>
  <td style="padding:10px 0 10px 16px;border-bottom:1px solid #f0f0f0;vertical-align:top;">
    {unread_dot}<span style="font-weight:600;font-size:14px;color:#1a237e;">{_esc(item.subject)}</span><br>
    <span style="font-size:13px;color:#555;line-height:1.4;">{_esc(item.snippet[:100])}</span>
  </td>
</tr>"""

    overflow = ""
    if len(items) > MAX_NEEDS_REPLY:
        overflow = f'<p style="color:#888;font-size:13px;margin:8px 0 0;">+ {len(items) - MAX_NEEDS_REPLY} more…</p>'

    return _card(
        title="✉️ Needs Reply",
        color="#d32f2f",
        body=f'<table style="width:100%;border-collapse:collapse;">{rows}</table>{overflow}',
    )


def _section_fyi(items: list[TriageResult]) -> str:
    return _card(
        title="📄 FYI",
        color="#1565c0",
        body=f'<p style="color:#555;font-size:14px;margin:0;">'
             f'{len(items)} informational email{"s" if len(items) != 1 else ""} '
             f'(receipts, confirmations, updates) — no action needed.</p>',
    )


def _section_promotions(
    items: list[TriageResult],
    candidates: list[UnsubscribeCandidate],
) -> str:
    body = ""
    if items:
        body += (
            f'<p style="color:#555;font-size:14px;margin:0 0 12px;">'
            f'{len(items)} promotional email{"s" if len(items) != 1 else ""} detected.</p>'
        )
    if candidates:
        body += '<p style="font-weight:600;font-size:13px;color:#444;margin:0 0 8px;">Unsubscribe candidates:</p>'
        for c in candidates[:MAX_UNSUB_CANDIDATES]:
            body += (
                f'<div style="padding:6px 0;border-bottom:1px solid #f5e0d3;">'
                f'<span style="font-weight:600;color:#333;">{_esc(c.display_name)}</span> '
                f'<span style="color:#888;font-size:12px;">({c.email_count} emails · {c.method_label})</span>'
                f'</div>'
            )
        if len(candidates) > MAX_UNSUB_CANDIDATES:
            body += f'<p style="color:#888;font-size:12px;margin:6px 0 0;">+ {len(candidates) - MAX_UNSUB_CANDIDATES} more…</p>'

    return _card(title="🏷 Promotions", color="#e65100", body=body)


def _section_junk(
    items: list[TriageResult],
    groups: list[CleanupGroup],
) -> str:
    body = ""
    if items:
        body += (
            f'<p style="color:#555;font-size:14px;margin:0 0 12px;">'
            f'{len(items)} junk email{"s" if len(items) != 1 else ""} detected.</p>'
        )
    if groups:
        body += '<p style="font-weight:600;font-size:13px;color:#444;margin:0 0 8px;">Cleanup groups pending confirmation:</p>'
        for g in groups[:MAX_CLEANUP_GROUPS]:
            score_color = {"high": "#c62828", "medium": "#e65100", "low": "#2e7d32"}.get(g.score_label, "#555")
            body += (
                f'<div style="padding:6px 0;border-bottom:1px solid #ede0dc;">'
                f'<span style="font-weight:600;color:#333;">{_esc(g.label)}</span> '
                f'<span style="color:#888;font-size:12px;">({g.email_count} emails)</span> '
                f'<span style="color:{score_color};font-size:11px;font-weight:600;text-transform:uppercase;">'
                f'{g.score_label}</span>'
                f'</div>'
            )
        if len(groups) > MAX_CLEANUP_GROUPS:
            body += f'<p style="color:#888;font-size:12px;margin:6px 0 0;">+ {len(groups) - MAX_CLEANUP_GROUPS} more…</p>'

    return _card(title="🗑 Junk", color="#4e342e", body=body)


def _section_unclassified(items: list[TriageResult]) -> str:
    rows = ""
    for item in items[:MAX_UNCLASSIFIED]:
        pct = f"{item.confidence:.0%}"
        rows += (
            f'<div style="padding:7px 0;border-bottom:1px solid #f0f0f0;">'
            f'<span style="font-weight:600;font-size:13px;color:#333;">{_esc(item.subject[:60])}</span>'
            f'<span style="color:#aaa;font-size:11px;margin-left:8px;">{_esc(item.sender_email)} · {pct} confident</span><br>'
            f'<span style="font-size:12px;color:#888;">{_esc(item.reason[:100])}</span>'
            f'</div>'
        )
    overflow = ""
    if len(items) > MAX_UNCLASSIFIED:
        overflow = f'<p style="color:#888;font-size:12px;margin:8px 0 0;">+ {len(items) - MAX_UNCLASSIFIED} more…</p>'

    return _card(
        title="❓ Needs Review",
        color="#6a1b9a",
        body=f"<p style='color:#555;font-size:13px;margin:0 0 10px;'>These were classified with low confidence. Please review:</p>{rows}{overflow}",
    )


def _footer(
    run_id: str,
    has_actions: bool,
    dashboard_url: Optional[str] = None,
    session_path: str = "output/pending_actions.json",
) -> str:
    action_block = ""
    if has_actions:
        path = _esc(session_path)
        dashboard_link = ""
        if dashboard_url:
            url = html_lib.escape(dashboard_url.rstrip("/") + "/pending", quote=True)
            dashboard_link = f"""
  <p style="margin:0 0 10px;">
    <a href="{url}" style="display:inline-block;background:#2e7d32;color:#fff;text-decoration:none;padding:8px 16px;border-radius:5px;font-size:13px;font-weight:600;">Review in dashboard →</a>
  </p>
  <p style="margin:0;font-size:12px;color:#777;">Or from the command line:</p>"""
        action_block = f"""
<div style="background:#e8f5e9;border:1px solid #a5d6a7;border-radius:6px;padding:14px 18px;margin:16px 0;">
  <p style="margin:0 0 6px;font-weight:600;font-size:14px;color:#2e7d32;">Actions pending your confirmation</p>{dashboard_link}
  <p style="margin:0;font-size:13px;color:#555;">
    Open <code style="background:#f1f1f1;padding:2px 5px;border-radius:3px;">{path}</code>,
    set <code style="background:#f1f1f1;padding:2px 5px;border-radius:3px;">"confirmed": true</code>
    on the items you want to process, then run:
  </p>
  <pre style="background:#1a1a2e;color:#a5d6a7;padding:12px 16px;border-radius:5px;margin:10px 0 0;font-size:13px;overflow-x:auto;">python main.py execute --session {path}</pre>
</div>
"""

    return f"""
{action_block}
<div style="margin-top:24px;padding-top:16px;border-top:1px solid #eee;text-align:center;">
  <p style="color:#bbb;font-size:12px;margin:0;">
    GmailCleaner &nbsp;·&nbsp; Run <code>{run_id}</code> &nbsp;·&nbsp;
    <a href="https://github.com/akadeepesh/GmailCleaner" style="color:#bbb;">GitHub</a>
  </p>
</div>
"""


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _card(title: str, color: str, body: str) -> str:
    return f"""
<div style="margin:16px 0;border:1px solid #e0e0e0;border-radius:8px;overflow:hidden;">
  <div style="background:{color};padding:10px 18px;">
    <h2 style="margin:0;font-size:15px;font-weight:700;color:#fff;">{title}</h2>
  </div>
  <div style="padding:16px 18px;background:#fff;">
    {body}
  </div>
</div>
"""


def _wrap_html(body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GmailCleaner Digest</title>
</head>
<body style="margin:0;padding:20px;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:640px;margin:0 auto;background:#f5f5f5;">
  {body}
</div>
</body>
</html>"""


def _esc(text: str) -> str:
    """Minimal HTML escaping for user-supplied strings."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


def _compose_raw(to: str, subject: str, html: str) -> str:
    """
    Compose a MIME multipart email and return the base64url-encoded raw string
    expected by the Gmail API users.messages.send endpoint.
    """
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    msg["From"] = to  # Sending to self; From = To.

    # Plain-text fallback for email clients that don't render HTML.
    plain = (
        f"GmailCleaner Daily Digest\n"
        f"Open pending_actions.json and run: python main.py execute\n"
        f"(This email is best viewed in an HTML-capable email client.)"
    )
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")