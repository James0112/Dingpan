from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import TEMPLATE_DIR

try:
    import resend
except ModuleNotFoundError:  # pragma: no cover - optional during local setup
    resend = None


class MailerError(RuntimeError):
    pass


_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(("html", "xml")),
)


def render_template(template_name: str, context: dict[str, object]) -> str:
    return _env.get_template(template_name).render(**context)


def send_resend_email(
    *,
    api_key: str | None,
    from_email: str | None,
    to_email: str,
    subject: str,
    html: str,
    text: str,
) -> None:
    if resend is None:
        raise MailerError("resend is not installed. Run `pip install -r requirements.txt`.")
    if not api_key:
        raise MailerError("RESEND_API_KEY is not configured.")
    if not from_email:
        raise MailerError("MAIL_FROM_AUTH or MAIL_FROM_REPORTS is not configured.")
    resend.api_key = api_key
    response = resend.Emails.send(
        {
            "from": from_email,
            "to": [to_email],
            "subject": subject,
            "html": html,
            "text": text,
        }
    )
    email_id = None
    if isinstance(response, dict):
        email_id = response.get("id")
    else:
        email_id = getattr(response, "id", None)
        if email_id is None:
            data = getattr(response, "data", None)
            email_id = getattr(data, "id", None)
    if email_id:
        return
    raise MailerError(f"Unexpected Resend response: {response!r}")
