"""SMTP sender for helpdesk@deepiri.com — a Gmail account reached via standard
SMTP (Cloudflare only routes inbound mail for the domain; outbound send still
goes through Gmail's SMTP with an app password on that account).
"""

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Optional


logger = logging.getLogger("deepiri.emailer")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "Deepiri Support <helpdesk@deepiri.com>")


def is_configured() -> bool:
    return bool(SMTP_USERNAME and SMTP_PASSWORD)


def send_email(to_email: str, subject: str, body: str) -> bool:
    """Best-effort synchronous send — call via asyncio.to_thread from async code.
    Returns False (never raises) on any failure so callers can fall back cleanly."""
    if not is_configured():
        logger.error("Cannot send email: SMTP_USERNAME/SMTP_PASSWORD not configured")
        return False
    if not to_email:
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to_email)
        return False
