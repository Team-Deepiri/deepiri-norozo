"""emailer.py's proxy-tunneled SMTP path -- Render blocks outbound SMTP
entirely (confirmed live: OSError [Errno 101] Network is unreachable), so
sending has to route through deepiri-proxy's HTTP CONNECT tunnel instead of
dialing smtp.gmail.com directly."""

from unittest.mock import MagicMock, patch

import emailer


def test_is_configured_false_without_credentials(monkeypatch):
    monkeypatch.setattr(emailer, "SMTP_USERNAME", "")
    monkeypatch.setattr(emailer, "SMTP_PASSWORD", "")
    assert emailer.is_configured() is False


def test_is_configured_true_with_credentials(monkeypatch):
    monkeypatch.setattr(emailer, "SMTP_USERNAME", "user@example.com")
    monkeypatch.setattr(emailer, "SMTP_PASSWORD", "app-password")
    assert emailer.is_configured() is True


def test_send_email_uses_direct_smtp_when_no_proxy_configured(monkeypatch):
    monkeypatch.setattr(emailer, "SMTP_USERNAME", "user@example.com")
    monkeypatch.setattr(emailer, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(emailer, "DISCORD_PROXY_URL", None)

    fake_server = MagicMock()
    fake_server.__enter__ = MagicMock(return_value=fake_server)
    fake_server.__exit__ = MagicMock(return_value=False)

    with patch("emailer.smtplib.SMTP", return_value=fake_server) as smtp_ctor:
        sent, reason = emailer.send_email("to@example.com", "subject", "body")

    assert sent is True
    assert reason is None
    smtp_ctor.assert_called_once_with(emailer.SMTP_HOST, emailer.SMTP_PORT, timeout=20)
    fake_server.starttls.assert_called_once()
    fake_server.login.assert_called_once_with("user@example.com", "app-password")
    fake_server.send_message.assert_called_once()


def test_send_email_uses_proxied_smtp_when_proxy_configured(monkeypatch):
    monkeypatch.setattr(emailer, "SMTP_USERNAME", "user@example.com")
    monkeypatch.setattr(emailer, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(emailer, "DISCORD_PROXY_URL", "http://proxyuser:proxypass@1.2.3.4:8888")

    fake_server = MagicMock()
    fake_server.__enter__ = MagicMock(return_value=fake_server)
    fake_server.__exit__ = MagicMock(return_value=False)

    with patch("emailer._ProxiedSMTP", return_value=fake_server) as proxied_ctor:
        sent, reason = emailer.send_email("to@example.com", "subject", "body")

    assert sent is True
    assert reason is None
    proxied_ctor.assert_called_once_with(
        proxy_host="1.2.3.4",
        proxy_port=8888,
        proxy_user="proxyuser",
        proxy_pass="proxypass",
        target_host=emailer.SMTP_HOST,
        target_port=emailer.SMTP_PORT,
        timeout=20,
    )
    fake_server.login.assert_called_once_with("user@example.com", "app-password")


def test_send_email_returns_false_without_credentials(monkeypatch):
    monkeypatch.setattr(emailer, "SMTP_USERNAME", "")
    monkeypatch.setattr(emailer, "SMTP_PASSWORD", "")
    sent, reason = emailer.send_email("to@example.com", "subject", "body")
    assert sent is False
    assert reason == "SMTP not configured"


def test_send_email_returns_false_on_send_exception(monkeypatch):
    monkeypatch.setattr(emailer, "SMTP_USERNAME", "user@example.com")
    monkeypatch.setattr(emailer, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(emailer, "DISCORD_PROXY_URL", None)

    with patch("emailer.smtplib.SMTP", side_effect=OSError("Network is unreachable")):
        sent, reason = emailer.send_email("to@example.com", "subject", "body")

    assert sent is False
    assert "Network is unreachable" in reason


def test_send_email_surfaces_gmail_auth_failure_reason(monkeypatch):
    """The actual daev1005/mahlaka incident: Gmail rejected the app password
    with a 535 auth error. This must come back as a specific, actionable
    reason -- not a generic "failed" -- since a credentials failure breaks
    every future send until someone rotates SMTP_PASSWORD, unlike a one-off
    network blip."""
    import smtplib

    monkeypatch.setattr(emailer, "SMTP_USERNAME", "user@example.com")
    monkeypatch.setattr(emailer, "SMTP_PASSWORD", "stale-password")
    monkeypatch.setattr(emailer, "DISCORD_PROXY_URL", None)

    fake_server = MagicMock()
    fake_server.__enter__ = MagicMock(return_value=fake_server)
    fake_server.__exit__ = MagicMock(return_value=False)
    fake_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    with patch("emailer.smtplib.SMTP", return_value=fake_server):
        sent, reason = emailer.send_email("to@example.com", "subject", "body")

    assert sent is False
    assert "credentials" in reason.lower()
    assert "535" in reason


def test_proxied_smtp_get_socket_raises_on_non_200_connect(monkeypatch):
    """A CONNECT tunnel failure must surface as a real error, not silently
    hand back a broken socket."""

    class _FakeSocket:
        def __init__(self):
            self.sent = b""

        def sendall(self, data):
            self.sent += data

        def settimeout(self, t):
            pass

        def recv(self, n):
            return b"HTTP/1.1 403 Forbidden\r\n\r\n"

        def close(self):
            pass

    fake_sock = _FakeSocket()
    with patch("emailer.socket.create_connection", return_value=fake_sock):
        proxy = emailer._ProxiedSMTP.__new__(emailer._ProxiedSMTP)
        proxy._proxy_host = "1.2.3.4"
        proxy._proxy_port = 8888
        proxy._proxy_user = "u"
        proxy._proxy_pass = "p"
        try:
            proxy._get_socket("smtp.gmail.com", 587, 10)
            raised = False
        except OSError:
            raised = True
    assert raised is True
