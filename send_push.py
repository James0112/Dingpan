from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone

from src.config import load_settings
from src.database import init_db
from src.logger import configure_logging
from src.push import (
    PushError,
    load_due_push_users,
    load_user_latest_report_target,
    mark_daily_push_sent,
    send_push_to_user,
)
from src.schedule import has_reached_clock_time
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
    parser = argparse.ArgumentParser(description="Dispatch scheduled DingPan daily report push notifications")
    parser.add_argument("--date", dest="trade_date", help="Trade date in YYYY-MM-DD format")
    parser.add_argument("--dry-run", action="store_true", help="Resolve due users without sending notifications")
    parser.add_argument("--force", action="store_true", help="Ignore schedule guard and dispatch immediately")
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
    schedule_enforced = not args.force and not args.trade_date
    if schedule_enforced and not has_reached_clock_time(
        now_utc=now_utc,
        timezone_name=settings.report_schedule_timezone,
        clock_time=settings.report_push_time,
    ):
        logger.info(
            "Skip push dispatch: waiting for REPORT_PUSH_TIME=%s %s",
            settings.report_push_time,
            settings.report_schedule_timezone,
        )
        return 0

    due_users = await load_due_push_users(
        settings.db_path,
        trade_date=trade_date_text,
    )
    logger.info("Resolved %s due users for trade_date=%s", len(due_users), trade_date_text)
    if not due_users:
        return 0

    sent_count = 0
    failed_count = 0
    for due_user in due_users:
        report_target = await load_user_latest_report_target(
            settings.db_path,
            user_id=due_user.user_id,
            trade_date=trade_date_text,
        )
        if report_target is None:
            logger.info("Skip user %s: no active report target for %s", due_user.email, trade_date_text)
            continue

        report_url = (
            f"{settings.site_url.rstrip('/')}/report/"
            f"{report_target['stock_code']}/{trade_date_text}"
        )
        payload = {
            "title": f"{report_target['stock_name'] or report_target['stock_code']} 日报已生成",
            "body": f"{trade_date_text} 的盯盘日报已更新，点击查看完整分析与操作建议。",
            "tag": f"dingpan-report-{trade_date_text}-{report_target['stock_code']}",
            "stock_code": report_target["stock_code"],
            "trade_date": trade_date_text,
            "timestamp": int(now_utc.timestamp() * 1000),
            "renotify": True,
            "url": report_url,
        }
        logger.info(
            "Dispatch push to %s for %s",
            due_user.email,
            report_target["stock_code"],
        )
        if args.dry_run:
            sent_count += 1
            continue
        try:
            delivered = await send_push_to_user(
                settings.db_path,
                user_id=due_user.user_id,
                payload=payload,
                public_key=settings.vapid_public_key,
                private_key=settings.vapid_private_key,
                claims_email=settings.vapid_claims_email,
            )
            await mark_daily_push_sent(
                settings.db_path,
                user_id=due_user.user_id,
                trade_date=trade_date_text,
                sent_at_iso=now_utc.isoformat(),
            )
            logger.info("Push delivered to %s device(s) for user %s", delivered, due_user.email)
            sent_count += 1
        except PushError as exc:
            logger.error("Push failed for user %s: %s", due_user.email, exc)
            failed_count += 1

    logger.info("Scheduled push dispatch completed: sent=%s failed=%s", sent_count, failed_count)
    return 0 if failed_count == 0 else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
