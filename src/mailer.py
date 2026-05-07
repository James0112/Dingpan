from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import TEMPLATE_DIR
from src.database import connect, execute, fetch_all
from src.render_email import render_email as render_report_email
from src.render_report import analysis_result_from_json, build_report_context, market_data_from_json, news_list_from_json

try:
    import resend
except ModuleNotFoundError:  # pragma: no cover - optional during local setup
    resend = None


class MailerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DueEmailUser:
    user_id: int
    email: str
    push_timezone: str
    daily_push_time: str


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


def send_report_email(
    *,
    api_key: str | None,
    from_email: str | None,
    receiver_emails: list[str],
    subject: str,
    html: str,
    text: str,
) -> None:
    if not receiver_emails:
        raise MailerError("At least one report email receiver is required.")
    if resend is None:
        raise MailerError("resend is not installed. Run `pip install -r requirements.txt`.")
    if not api_key:
        raise MailerError("RESEND_API_KEY is not configured.")
    if not from_email:
        raise MailerError("MAIL_FROM_REPORTS is not configured.")
    resend.api_key = api_key
    response = resend.Emails.send(
        {
            "from": from_email,
            "to": receiver_emails,
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


def is_user_due_for_email(*, now_utc: datetime, push_timezone: str, daily_push_time: str, window_minutes: int) -> bool:
    local_now = now_utc.astimezone(ZoneInfo(push_timezone))
    try:
        target_hour, target_minute = [int(part) for part in daily_push_time.split(":", 1)]
    except ValueError:
        return False
    current_minutes = local_now.hour * 60 + local_now.minute
    target_minutes = target_hour * 60 + target_minute
    return target_minutes <= current_minutes < target_minutes + window_minutes


async def load_due_email_users(db_path: str, *, now_utc: datetime, window_minutes: int, trade_date: str) -> list[DueEmailUser]:
    rows = await fetch_all(
        db_path,
        """
        SELECT DISTINCT
            u.id,
            u.email,
            u.push_timezone,
            u.daily_push_time
        FROM users u
        INNER JOIN subscriptions s ON s.user_id = u.id AND s.is_active = 1
        INNER JOIN analysis_cache ac
            ON ac.stock_code = s.stock_code
            AND ac.model_id = s.model_id
            AND ac.trade_date = ?
            AND ac.status = 'success'
        WHERE u.is_active = 1
          AND u.email_verified_at != ''
          AND u.daily_email_enabled = 1
        """,
        (trade_date,),
    )
    due_users: list[DueEmailUser] = []
    for row in rows:
        if not is_user_due_for_email(
            now_utc=now_utc,
            push_timezone=str(row["push_timezone"]),
            daily_push_time=str(row["daily_push_time"]),
            window_minutes=window_minutes,
        ):
            continue
        due_users.append(
            DueEmailUser(
                user_id=int(row["id"]),
                email=str(row["email"]),
                push_timezone=str(row["push_timezone"]),
                daily_push_time=str(row["daily_push_time"]),
            )
        )
    return due_users


async def load_user_email_targets(db_path: str, *, user_id: int, trade_date: str) -> list[dict[str, object]]:
    rows = await fetch_all(
        db_path,
        """
        SELECT
            s.stock_code,
            s.stock_name,
            s.cost_price,
            s.model_id,
            ac.market_data_json,
            ac.analysis_json,
            ac.news_json
        FROM subscriptions s
        INNER JOIN analysis_cache ac
            ON ac.stock_code = s.stock_code
            AND ac.model_id = s.model_id
            AND ac.trade_date = ?
            AND ac.status = 'success'
        LEFT JOIN email_deliveries ed
            ON ed.user_id = s.user_id
            AND ed.stock_code = s.stock_code
            AND ed.trade_date = ?
            AND ed.delivery_type = 'daily_report'
        WHERE s.user_id = ?
          AND s.is_active = 1
          AND ed.id IS NULL
        ORDER BY s.sort_order ASC, s.id ASC
        """,
        (trade_date, trade_date, user_id),
    )
    return [dict(row) for row in rows]


async def mark_daily_report_delivered(
    db_path: str,
    *,
    user_id: int,
    stock_code: str,
    trade_date: str,
    sent_at_iso: str,
) -> None:
    await execute(
        db_path,
        """
        INSERT OR IGNORE INTO email_deliveries (user_id, stock_code, trade_date, delivery_type, sent_at)
        VALUES (?, ?, ?, 'daily_report', ?)
        """,
        (user_id, stock_code, trade_date, sent_at_iso),
    )


def send_daily_report_for_target(
    *,
    api_key: str | None,
    from_email: str | None,
    receiver_email: str,
    output_dir: Path,
    template_path: Path,
    target: dict[str, object],
    trade_date_text: str,
    now_local: datetime,
) -> str:
    market_data = market_data_from_json(str(target["market_data_json"]))
    analysis = analysis_result_from_json(str(target["analysis_json"]))
    news_list = news_list_from_json(str(target["news_json"]))
    context = build_report_context(
        market_data,
        analysis,
        news_list,
        cost_price=float(target["cost_price"]),
        model_id=str(target["model_id"]),
        previous_trade_date=None,
        next_trade_date=None,
    )
    subject, _, html_content = render_report_email(
        template_path=template_path,
        output_dir=output_dir,
        market_data=context["market_data"],
        analysis=analysis,
        cost_analysis=context["cost_analysis"],
        news_list=news_list,
        generated_at=now_local,
    )
    text_content = (
        f"{subject}\n\n"
        f"{context['market_data'].stock_name}({context['market_data'].stock_code}) {trade_date_text} 报告已生成。\n"
        "请在支持 HTML 的邮箱中查看完整内容。"
    )
    send_report_email(
        api_key=api_key,
        from_email=from_email,
        receiver_emails=[receiver_email],
        subject=subject,
        html=html_content,
        text=text_content,
    )
    return str(target["stock_code"])
