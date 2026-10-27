"""Sending email (password resets) through any SMTP provider.

Configure SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = TLS), SMTP_USERNAME, SMTP_PASSWORD and
MAIL_FROM, e.g. "VibeGrind <hello@vibegrind.app>". Without SMTP_HOST, email features are off.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid


class MailError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("MAIL_FROM"))


def send(to: str, subject: str, text: str) -> None:
    if not available():
        raise MailError("Email isn't configured on this server.")
    msg = EmailMessage()
    msg["From"] = os.environ["MAIL_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=os.environ["MAIL_FROM"].rsplit("@", 1)[-1].strip(" >"))
    msg.set_content(text)

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, password = os.environ.get("SMTP_USERNAME"), os.environ.get("SMTP_PASSWORD")
    context = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls(context=context)
        with server:
            if user:
                server.login(user, password or "")
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Couldn't send email ({type(e).__name__}).") from e
