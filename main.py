from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime
from zoneinfo import ZoneInfo

from src.analyze import AnalysisError, analyze_market_data
from src.config import load_settings
from src.fetch_data import DataFetchError, fetch_market_data
from src.fetch_news import fetch_news
from src.logger import configure_logging
from src.render_email import render_email
from src.send_email import EmailSendError, send_email
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
    parser = argparse.ArgumentParser(description="Generate DingPan daily stock report")
    parser.add_argument("--preview", action="store_true", help="Generate the report and open the HTML preview locally")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logger = configure_logging()
    settings = load_settings()
    now = datetime.now(ZoneInfo(settings.timezone_name))
    today = get_today(settings.timezone_name)

    logger.info("Starting DingPan for %s (%s)", settings.stock_code, settings.stock_name)

    if settings.disable_proxy:
        cleared = []
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            if os.environ.pop(key, None) is not None:
                cleared.append(key)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        if cleared:
            logger.info("Cleared proxy env vars for market data requests: %s", ", ".join(cleared))

    try:
        trade_dates = load_trade_calendar()
        if is_calendar_stale(today, trade_dates):
            raise TradingCalendarError("Trade calendar is stale for the current date")
        if not is_today_trading_day(today, trade_dates):
            logger.info("Today %s is not an A-share trading day. Skip sending.", today)
            return 0
        use_same_day_for_test = (settings.dry_run or args.preview) and now.hour >= 15
        if use_same_day_for_test:
            latest_trade_date = today
            logger.info("Using same-day trade date for manual test after market close: %s", latest_trade_date)
        else:
            latest_trade_date = get_latest_trade_date(today, trade_dates)
            logger.info("Resolved latest trade date from calendar: %s", latest_trade_date)
    except TradingCalendarError as exc:
        logger.warning("Trading calendar unavailable or outdated: %s", exc)
        if today.weekday() >= 5:
            logger.info("Fallback weekday check says today is weekend. Skip sending.")
            return 0
        use_same_day_for_test = (settings.dry_run or args.preview) and now.hour >= 15
        if use_same_day_for_test:
            latest_trade_date = today
            logger.warning("Fallback to same-day trade date for manual test after market close: %s", latest_trade_date)
        else:
            latest_trade_date = fallback_latest_trade_date(today)
            logger.warning("Fallback latest trade date by weekday only: %s", latest_trade_date)

    try:
        market_data = fetch_market_data(
            stock_code=settings.stock_code,
            stock_name=settings.stock_name,
            cost_price=settings.cost_price,
            latest_trade_date=latest_trade_date,
        )
        logger.info("Fetched market data successfully")
    except DataFetchError as exc:
        logger.error("Market data fetch failed: %s", exc)
        return 1

    try:
        news_list = fetch_news(
            stock_code=settings.stock_code,
            latest_trade_date=latest_trade_date,
            lookback_hours=settings.news_lookback_hours,
            max_items=settings.max_news_items,
        )
        logger.info("Fetched %s news items", len(news_list))
    except Exception as exc:  # pragma: no cover - unexpected runtime
        logger.warning("News fetch failed, continuing without news: %s", exc)
        news_list = []

    try:
        analysis = analyze_market_data(
            api_key=settings.gemini_api_key or "",
            model_name=settings.model_name,
            market_data=market_data,
            news_list=news_list,
            fallback_model_name=settings.fallback_model_name,
        )
        logger.info("Gemini analysis completed")
    except AnalysisError as exc:
        logger.error("Analysis failed: %s", exc)
        return 1

    subject, html_path, html_content = render_email(
        template_path=settings.template_path,
        output_dir=settings.output_dir,
        market_data=market_data,
        analysis=analysis,
        news_list=news_list,
        generated_at=now,
    )
    logger.info("Rendered HTML report to %s", html_path)

    if settings.dry_run or args.preview:
        logger.info("Dry run enabled. Skip sending email.")
        if args.preview:
            try:
                webbrowser.open(html_path.resolve().as_uri())
            except Exception as exc:  # pragma: no cover - GUI/runtime dependent
                logger.warning("Failed to open preview automatically: %s", exc)
        return 0

    try:
        send_email(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            sender_email=settings.qq_email or "",
            sender_auth_code=settings.qq_email_auth_code or "",
            receiver_emails=list(settings.receiver_emails),
            subject=subject,
            html_content=html_content,
        )
        logger.info("Email sent successfully to %s", ", ".join(settings.receiver_emails))
        return 0
    except EmailSendError as exc:
        logger.error("Email send failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
