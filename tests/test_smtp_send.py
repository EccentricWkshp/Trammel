"""Sending through a real SMTP connection, built from the email settings (FR-NTF-1).

The rest of the suite swaps in Django's in-memory backend (TRAMMEL_EMAIL_BACKEND),
so the connection Trammel builds for a real server was never made in a test,
and a Django 6.1 change broke it unnoticed. Here a tiny SMTP server runs in a
thread and the real backend talks to it.
"""

import socketserver
import threading

import pytest
from django.test import override_settings

from trammel.core import secrets
from trammel.notifications import services
from trammel.notifications.models import SmtpSettings


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        def say(line):
            self.wfile.write((line + "\r\n").encode())

        say("220 test ESMTP")
        in_data, lines = False, []
        while raw := self.rfile.readline():
            text = raw.decode().rstrip("\r\n")
            if in_data:
                if text == ".":
                    self.server.messages.append("\n".join(lines))
                    in_data, lines = False, []
                    say("250 OK")
                else:
                    lines.append(text)
                continue
            command = text[:4].upper()
            self.server.commands.append(command)
            if command in ("EHLO", "HELO"):
                say("250-test")
                say("250 AUTH PLAIN")
            elif command == "AUTH":
                say("235 Authentication succeeded")
            elif command == "DATA":
                in_data = True
                say("354 End data with <CR><LF>.<CR><LF>")
            elif command == "QUIT":
                say("221 Bye")
                break
            else:
                say("250 OK")


@pytest.fixture
def smtp_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    server.daemon_threads = True
    server.messages, server.commands = [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.mark.django_db
@override_settings(TRAMMEL_EMAIL_BACKEND="")
def test_test_email_goes_through_a_real_smtp_connection(admin_client, smtp_server):
    smtp = SmtpSettings.load()
    smtp.host, smtp.port = "127.0.0.1", smtp_server.server_address[1]
    smtp.security, smtp.username = SmtpSettings.Security.NONE, "shop"
    smtp.from_address, smtp.from_name = "shop@example.com", "Test Shop"
    smtp.save()
    secrets.put(services.PASSWORD_KEY, "pw")

    services.send_test("owner@example.com")

    assert "AUTH" in smtp_server.commands
    assert len(smtp_server.messages) == 1
    message = smtp_server.messages[0]
    assert "Subject: Trammel test email" in message and "To: owner@example.com" in message
    assert "From: Test Shop <shop@example.com>" in message


@pytest.mark.django_db
@override_settings(TRAMMEL_EMAIL_BACKEND="")
def test_connection_takes_every_option_from_the_email_settings(setup_done):
    smtp = SmtpSettings.load()
    smtp.host, smtp.port, smtp.security = "mail.example.com", 465, SmtpSettings.Security.SSL
    smtp.save()
    backend = services.connection()
    assert (backend.host, backend.port, backend.use_ssl, backend.use_tls) == (
        "mail.example.com",
        465,
        True,
        False,
    )
    assert backend.ssl_keyfile is None and backend.ssl_certfile is None
