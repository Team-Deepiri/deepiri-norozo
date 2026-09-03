"""SMTP sender for helpdesk@deepiri.com — a Gmail account reached via standard
SMTP (Cloudflare only routes inbound mail for the domain; outbound send still
goes through Gmail's SMTP with an app password on that account).

Render blocks outbound SMTP entirely (confirmed live: OSError [Errno 101]
Network is unreachable connecting to smtp.gmail.com:587/465 — a network-level
block, not a credentials issue). Routed through deepiri-proxy instead: the same
tinyproxy sidecar already used for Discord's REST/gateway traffic during the
Cloudflare 1015 egress ban (see DISCORD_PROXY_URL / main.py's
_discord_proxy_kwargs). tinyproxy has no ConnectPort restriction configured, so
it'll CONNECT-tunnel to any port including 587 — confirmed working end-to-end
(TCP reachability, then a full STARTTLS+login) once the VPS's own former
netcup-level SMTP block was lifted. Reuses DISCORD_PROXY_URL as-is; no separate
SMTP-proxy env var needed.
"""

import base64
import logging
import os
import smtplib
import socket
from email.message import EmailMessage
from urllib.parse import urlparse


logger = logging.getLogger("deepiri.emailer")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "Deepiri Support <helpdesk@deepiri.com>")
DISCORD_PROXY_URL = (os.getenv("DISCORD_PROXY_URL") or "").strip() or None


def is_configured() -> bool:
    return bool(SMTP_USERNAME and SMTP_PASSWORD)


class _ProxiedSMTP(smtplib.SMTP):
    """smtplib.SMTP that tunnels its connection through an HTTP CONNECT proxy
    instead of dialing the target host directly -- everything else (STARTTLS,
    auth, message send) happens exactly as normal on top of the tunneled socket.
    """

    def __init__(self, proxy_host: str, proxy_port: int, proxy_user: str, proxy_pass: str, target_host: str, target_port: int, timeout: int = 20):
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._proxy_user = proxy_user
        self._proxy_pass = proxy_pass
        super().__init__(target_host, target_port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        sock = socket.create_connection((self._proxy_host, self._proxy_port), timeout=timeout)
        auth = base64.b64encode(f"{self._proxy_user}:{self._proxy_pass}".encode()).decode()
        request = (
            f"CONNECT {host}:{port} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Proxy-Authorization: Basic {auth}\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode())
        sock.settimeout(timeout)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        status_line = response.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200 " not in status_line:
            sock.close()
            raise OSError(f"Proxy CONNECT to {host}:{port} failed: {status_line!r}")
        return sock


def _open_smtp_connection() -> smtplib.SMTP:
    if DISCORD_PROXY_URL:
        parsed = urlparse(DISCORD_PROXY_URL)
        server = _ProxiedSMTP(
            proxy_host=parsed.hostname,
            proxy_port=parsed.port or 8888,
            proxy_user=parsed.username or "",
            proxy_pass=parsed.password or "",
            target_host=SMTP_HOST,
            target_port=SMTP_PORT,
            timeout=20,
        )
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
    return server


def send_email(to_email: str, subject: str, body: str) -> tuple:
    """Best-effort synchronous send — call via asyncio.to_thread from async code.
    Returns (True, None) on success or (False, short_reason) on any failure
    (never raises) so callers can fall back cleanly AND surface *why* it
    failed -- e.g. "Gmail rejected credentials (535)" vs a generic network
    timeout are very different problems requiring very different fixes, and
    burying that distinction in a log line nobody's watching means the same
    root cause (an expired app password, say) silently recurs on every send
    until someone happens to go dig through Render logs."""
    if not is_configured():
        logger.error("Cannot send email: SMTP_USERNAME/SMTP_PASSWORD not configured")
        return False, "SMTP not configured"
    if not to_email:
        return False, "no recipient address"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with _open_smtp_connection() as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        logger.exception("Failed to send email to %s", to_email)
        return False, f"Gmail rejected credentials ({e.smtp_code}) -- SMTP_PASSWORD likely expired/revoked, needs a fresh App Password"
    except (smtplib.SMTPException, OSError) as e:
        logger.exception("Failed to send email to %s", to_email)
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:
        logger.exception("Failed to send email to %s", to_email)
        return False, f"{type(e).__name__}: {e}"
