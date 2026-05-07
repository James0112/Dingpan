from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from src.config import load_settings
from src.database import init_db
from src.logger import configure_logging
from src.mailer import (
    MailerError,
    load_due_email_users,
    load_user_email_targets,
    mark_daily_report_delivered,
    send_daily_report_for_target,
)
from src.trading_calendar import (
    TradingCalendarError,
    fallback_latest_trade_date,
    get_latest_trade_date,
    get_today,
    is_calendar_stale,
    is_today_trading_day,
    load_trade_calendar,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch scheduled DingPan daily report emails")
    parser.add_argument("--date", dest="trade_date", help="Trade date in YYYY-MM-DD format")
    parser.add_argument("--window-minutes", type=int, default=10, help="Delivery window size per user local time")
    parser.add_argument("--dry-run", action="store_true", help="Resolve due users without sending emails")
    return parser.parse_args()


def resolve_trade_date(settings, explicit_trade_date: str | None) -> date:
    if explicit_trade_date:
        return date.fromisoformat(explicit_trade_date)
    today = get_today(settings.timezone_name)
    try:
        trade_dates = load_trade_calendar()
        if is_calendar_stale(today, trade_dates):
            raise TradingCalendarError("Trade calendar is stale")
        if not is_today_trading_day(today, trade_dates):
            return get_latest_trade_date(today, trade_dates)
        return get_latest_trade_date(today, trade_dates)
    except TradingCalendarError:
        return fallback_latest_trade_date(today)


async def run() -> int:
    args = parse_args()
    settings = load_settings()
    logger = configure_logging()
    await init_db(settings.db_path)

    trade_date_value = resolve_trade_date(settings, args.trade_date)
    trade_date_text = trade_date_value.isoformat()
    now_utc = datetime.now(timezone.utc)

    due_users = await load_due_email_users(
        settings.db_path,
        now_utc=now_utc,
        window_minutes=args.window_minutes,
        trade_date=trade_date_text,
    )
    logger.info("Resolved %s due email users for trade_date=%s", len(due_users), trade_date_text)
    if not due_users:
        return 0

    if not args.dry_run and (not settings.resend_api_key or not settings.mail_from_reports):
        logger.error("Resend report email configuration is incomplete")
        return 1

    sent_count = 0
    failed_count = 0
    for due_user in due_users:
        targets = await load_user_email_targets(
            settings.db_path,
            user_id=due_user.user_id,
            trade_date=trade_date_text,
        )
        if not targets:
            logger.info("Skip user %s: no unsent report targets for %s", due_user.email, trade_date_text)
            continue
        logger.info(
            "Dispatch %s report email(s) to %s at %s %s",
            len(targets),
            due_user.email,
            due_user.daily_push_time,
            due_user.push_timezone,
        )
        if args.dry_run:
            sent_count += len(targets)
            continue
        now_local = now_utc.astimezone(ZoneInfo(due_user.push_timezone))
        for target in targets:
            try:
                stock_code = await asyncio.to_thread(
                    send_daily_report_for_target,
                    api_key=settings.resend_api_key,
                    from_email=settings.mail_from_reports,
                    receiver_email=due_user.email,
                    output_dir=settings.output_dir,
                    template_path=settings.template_path,
                    target=target,
                    trade_date_text=trade_date_text,
                    now_local=now_local,
                )
                await mark_daily_report_delivered(
                    settings.db_path,
                    user_id=due_user.user_id,
                    stock_code=stock_code,
                    trade_date=trade_date_text,
                    sent_at_iso=now_utc.isoformat(),
                )
                sent_count += 1
            except MailerError as exc:
                logger.error("Report email failed for user %s stock %s: %s", due_user.email, target["stock_code"], exc)
                failed_count += 1

    logger.info("Scheduled report email dispatch completed: sent=%s failed=%s", sent_count, failed_count)
    return 0 if failed_count == 0 else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
