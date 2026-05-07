from __future__ import annotations

import argparse
import sys
from datetime import date

from src.analyze import AnalysisError, analyze_market_data
from src.config import load_settings
from src.database import connect, fetch_all, init_db
from src.fetch_data import DataFetchError, fetch_market_data
from src.fetch_news import fetch_news
from src.logger import configure_logging
from src.render_report import ANALYSIS_VERSION, analysis_result_to_json, market_data_to_json, news_list_to_json
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
    parser = argparse.ArgumentParser(description="Generate DingPan shared analysis cache")
    parser.add_argument("--date", dest="trade_date", help="Trade date in YYYY-MM-DD format")
    parser.add_argument("--stock", dest="stock_code", help="Only generate one stock code")
    parser.add_argument("--model", dest="model_id", help="Only generate one model id")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of unique stock/model pairs")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing analysis_cache")
    return parser.parse_args()


def resolve_trade_date(settings, args: argparse.Namespace) -> date:
    if args.trade_date:
        return date.fromisoformat(args.trade_date)
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


async def load_targets(db_path: str, stock_code: str | None, model_id: str | None, limit: int) -> list[dict[str, str]]:
    query = """
        SELECT DISTINCT stock_code, stock_name, model_id
        FROM subscriptions
        WHERE is_active = 1
    """
    params: list[object] = []
    if stock_code:
        query += " AND stock_code = ?"
        params.append(stock_code)
    if model_id:
        query += " AND model_id = ?"
        params.append(model_id)
    query += " ORDER BY stock_code ASC, model_id ASC"
    if limit > 0:
        query += f" LIMIT {int(limit)}"
    rows = await fetch_all(db_path, query, tuple(params))
    return [
        {
            "stock_code": str(row["stock_code"]),
            "stock_name": str(row["stock_name"]),
            "model_id": str(row["model_id"]),
        }
        for row in rows
    ]


async def upsert_analysis_cache(
    db_path: str,
    *,
    stock_code: str,
    stock_name: str,
    trade_date_value: date,
    model_id: str,
    status: str,
    error_message: str,
    market_data_json: str,
    analysis_json: str,
    news_json: str,
) -> None:
    conn = await connect(db_path)
    try:
        await conn.execute(
            """
            INSERT INTO analysis_cache (
                stock_code, stock_name, trade_date, model_id, analysis_version,
                status, error_message, market_data_json, analysis_json, news_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_code, trade_date, model_id) DO UPDATE SET
                stock_name=excluded.stock_name,
                analysis_version=excluded.analysis_version,
                status=excluded.status,
                error_message=excluded.error_message,
                market_data_json=excluded.market_data_json,
                analysis_json=excluded.analysis_json,
                news_json=excluded.news_json,
                created_at=CURRENT_TIMESTAMP
            """,
            (
                stock_code,
                stock_name,
                trade_date_value.isoformat(),
                model_id,
                ANALYSIS_VERSION,
                status,
                error_message,
                market_data_json,
                analysis_json,
                news_json,
            ),
        )
        await conn.commit()
    finally:
        await conn.close()


async def run() -> int:
    args = parse_args()
    settings = load_settings()
    logger = configure_logging()
    await init_db(settings.db_path)
    trade_date_value = resolve_trade_date(settings, args)
    logger.info("Resolved trade date for shared generation: %s", trade_date_value)

    targets = await load_targets(
        settings.db_path,
        stock_code=args.stock_code,
        model_id=args.model_id,
        limit=args.limit,
    )
    if not targets:
        logger.info("No active subscription targets found")
        return 0

    success_count = 0
    failure_count = 0
    for target in targets:
        stock_code = target["stock_code"]
        stock_name = target["stock_name"] or stock_code
        model_id = target["model_id"]
        logger.info("Generating shared analysis for %s (%s) with %s", stock_code, stock_name, model_id)
        try:
            market_data = fetch_market_data(
                stock_code=stock_code,
                stock_name=stock_name,
                cost_price=0.0,
                latest_trade_date=trade_date_value,
            )
            news_list = fetch_news(
                stock_code=stock_code,
                latest_trade_date=market_data.latest_trade_date,
                lookback_hours=settings.news_lookback_hours,
                max_items=settings.max_news_items,
            )
            analysis = analyze_market_data(
                api_key=settings.gemini_api_key or "",
                model_id=model_id,
                model_name=settings.model_name,
                market_data=market_data,
                news_list=news_list,
                fallback_model_name=settings.fallback_model_name,
            )
            if not args.dry_run:
                await upsert_analysis_cache(
                    settings.db_path,
                    stock_code=stock_code,
                    stock_name=market_data.stock_name,
                    trade_date_value=market_data.latest_trade_date,
                    model_id=model_id,
                    status="success",
                    error_message="",
                    market_data_json=market_data_to_json(market_data),
                    analysis_json=analysis_result_to_json(analysis),
                    news_json=news_list_to_json(news_list),
                )
            success_count += 1
        except (DataFetchError, AnalysisError, ValueError) as exc:
            logger.error("Shared analysis failed for %s/%s: %s", stock_code, model_id, exc)
            failure_count += 1
            if not args.dry_run:
                await upsert_analysis_cache(
                    settings.db_path,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    trade_date_value=trade_date_value,
                    model_id=model_id,
                    status="failed",
                    error_message=str(exc),
                    market_data_json="{}",
                    analysis_json="{}",
                    news_json="[]",
                )

    logger.info("Shared generation completed: success=%s failure=%s", success_count, failure_count)
    return 0 if failure_count == 0 else 1


def main() -> int:
    return __import__("asyncio").run(run())


if __name__ == "__main__":
    sys.exit(main())
