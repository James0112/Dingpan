from __future__ import annotations

import smtplib
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime
from zoneinfo import ZoneInfo


class EmailSendError(RuntimeError):
    pass


def send_email(
    smtp_host: str,
    smtp_port: int,
    sender_email: str,
    sender_auth_code: str,
    receiver_emails: list[str],
    subject: str,
    html_content: str,
) -> None:
    if not sender_email or not sender_auth_code or not receiver_emails:
        raise EmailSendError("Email credentials and at least one receiver email are required")

    message = MIMEMultipart("alternative")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = sender_email
    message["To"] = ", ".join(receiver_emails)
    message["Date"] = format_datetime(datetime.now(ZoneInfo("Asia/Shanghai")))
    message.attach(MIMEText(html_content, "html", "utf-8"))

    last_error: Exception | None = None
    for _ in range(2):
        try:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as smtp:
                smtp.login(sender_email, sender_auth_code)
                smtp.sendmail(sender_email, receiver_emails, message.as_string())
            return
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            last_error = exc
    raise EmailSendError(f"Failed to send email after retry: {last_error}")
