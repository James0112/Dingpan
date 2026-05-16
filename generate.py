from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from datetime import date, datetime, timezone
from queue import Empty

from src.analyze import AnalysisError, analyze_market_data
from src.config import load_settings
from src.database import connect, fetch_all, init_db
from src.fetch_data import DataFetchError, fetch_market_data
from src.fetch_news import fetch_news
from src.logger import configure_logging
from src.render_report import ANALYSIS_VERSION, analysis_result_to_json, market_data_to_json, news_list_to_json
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


def _build_failure_result(trade_date_value: date, error_message: str) -> dict[str, str]:
    return {
        "status": "failed",
        "error_message": error_message,
        "trade_date": trade_date_value.isoformat(),
        "market_data_json": "{}",
        "analysis_json": "{}",
        "news_json": "[]",
        "actual_provider": "",
        "actual_model_name": "",
        "provider_response_id": "",
    }


def _generate_target_worker(
    result_queue,
    settings,
    target: dict[str, str],
    trade_date_text: str,
) -> None:
    logger = configure_logging()
    stock_code = target["stock_code"]
    stock_name = target["stock_name"] or stock_code
    model_id = target["model_id"]
    requested_trade_date = date.fromisoformat(trade_date_text)
    try:
        logger.info("Fetching market data for %s (%s)", stock_code, stock_name)
        market_data = fetch_market_data(
            stock_code=stock_code,
            stock_name=stock_name,
            cost_price=0.0,
            latest_trade_date=requested_trade_date,
        )
        logger.info(
            "Fetched market data for %s (%s): trade_date=%s",
            stock_code,
            stock_name,
            market_data.latest_trade_date,
        )
        logger.info("Fetching news for %s (%s)", stock_code, stock_name)
        news_list = fetch_news(
            stock_code=stock_code,
            latest_trade_date=market_data.latest_trade_date,
            lookback_hours=settings.news_lookback_hours,
            max_items=settings.max_news_items,
        )
        logger.info("Fetched %s news item(s) for %s (%s)", len(news_list), stock_code, stock_name)
        logger.info("Generating AI analysis for %s (%s) with %s", stock_code, stock_name, model_id)
        analyze_output = analyze_market_data(
            model_id,
            market_data,
            news_list,
            db_path=settings.db_path,
            settings=settings,
        )
        result_queue.put(
            {
                "status": "success",
                "error_message": "",
                "trade_date": market_data.latest_trade_date.isoformat(),
                "market_data_json": market_data_to_json(market_data),
                "analysis_json": analysis_result_to_json(analyze_output.analysis),
                "news_json": news_list_to_json(news_list),
                "actual_provider": analyze_output.actual_provider,
                "actual_model_name": analyze_output.actual_model_name,
                "provider_response_id": analyze_output.provider_response_id,
            }
        )
    except (DataFetchError, AnalysisError, ValueError) as exc:
        result_queue.put(_build_failure_result(requested_trade_date, str(exc)))
    except Exception as exc:  # pragma: no cover - defensive guard
        result_queue.put(_build_failure_result(requested_trade_date, f"Unexpected error: {exc}"))


def _run_target_with_timeout(settings, target: dict[str, str], trade_date_value: date) -> dict[str, str]:
    stock_code = target["stock_code"]
    stock_name = target["stock_name"] or stock_code
    model_id = target["model_id"]
    timeout_seconds = settings.generate_target_timeout_seconds
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_generate_target_worker,
        args=(result_queue, settings, target, trade_date_value.isoformat()),
    )
    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        result_queue.close()
        result_queue.join_thread()
        return _build_failure_result(
            trade_date_value,
            (
                f"Timed out after {timeout_seconds}s while generating shared analysis "
                f"[stock={stock_code} model={model_id} name={stock_name}]"
            ),
        )

    try:
        payload = result_queue.get_nowait()
    except Empty:
        payload = _build_failure_result(
            trade_date_value,
            (
                f"Worker exited without result (exit_code={process.exitcode}) "
                f"[stock={stock_code} model={model_id} name={stock_name}]"
            ),
        )
    finally:
        result_queue.close()
        result_queue.join_thread()

    if payload["status"] == "failed":
        payload["error_message"] = (
            f"{payload['error_message']} "
            f"[stock={stock_code} model={model_id} name={stock_name} "
            f"provider={payload.get('actual_provider', '')} actual_model={payload.get('actual_model_name', '')}]"
        )
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate DingPan shared analysis cache")
    parser.add_argument("--date", dest="trade_date", help="Trade date in YYYY-MM-DD format")
    parser.add_argument("--stock", dest="stock_code", help="Only generate one stock code")
    parser.add_argument("--model", dest="model_id", help="Only generate one model id")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of unique stock/model pairs")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing analysis_cache")
    parser.add_argument("--force", action="store_true", help="Ignore schedule guard and regenerate existing success rows")
    parser.add_argument(
        "--today-if-trading-day",
        action="store_true",
        help="Use today's trade date when today is a trading day; otherwise fall back to the latest previous trade date",
    )
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
        if args.today_if_trading_day:
            return today
        return get_latest_trade_date(today, trade_dates)
    except TradingCalendarError:
        if args.today_if_trading_day and today.weekday() < 5:
            return today
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
    actual_provider: str,
    actual_model_name: str,
    provider_response_id: str,
) -> None:
    conn = await connect(db_path)
    try:
        await conn.execute(
            """
            INSERT INTO analysis_cache (
                stock_code, stock_name, trade_date, model_id, analysis_version,
                status, error_message, market_data_json, analysis_json, news_json,
                actual_provider, actual_model_name, provider_response_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stock_code, trade_date, model_id) DO UPDATE SET
                stock_name=excluded.stock_name,
                analysis_version=excluded.analysis_version,
                status=excluded.status,
                error_message=excluded.error_message,
                market_data_json=excluded.market_data_json,
                analysis_json=excluded.analysis_json,
                news_json=excluded.news_json,
                actual_provider=excluded.actual_provider,
                actual_model_name=excluded.actual_model_name,
                provider_response_id=excluded.provider_response_id,
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
                actual_provider,
                actual_model_name,
                provider_response_id,
            ),
        )
        await conn.commit()
    finally:
        await conn.close()


async def has_successful_cached_analysis(
    db_path: str,
    *,
    stock_code: str,
    trade_date_value: date,
    model_id: str,
) -> bool:
    row = await fetch_all(
        db_path,
        """
        SELECT 1
        FROM analysis_cache
        WHERE stock_code = ? AND trade_date = ? AND model_id = ? AND status = 'success'
        LIMIT 1
        """,
        (stock_code, trade_date_value.isoformat(), model_id),
    )
    return bool(row)


async def run() -> int:
    args = parse_args()
    settings = load_settings()
    logger = configure_logging()
    await init_db(settings.db_path)
    trade_date_value = resolve_trade_date(settings, args)
    logger.info("Resolved trade date for shared generation: %s", trade_date_value)
    schedule_enforced = not args.force and not args.trade_date and not args.stock_code and not args.model_id
    if schedule_enforced and not has_reached_clock_time(
        now_utc=datetime.now(timezone.utc),
        timezone_name=settings.report_schedule_timezone,
        clock_time=settings.report_generate_time,
    ):
        logger.info(
            "Skip shared generation: waiting for REPORT_GENERATE_TIME=%s %s",
            settings.report_generate_time,
            settings.report_schedule_timezone,
        )
        return 0

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
    skipped_count = 0
    for target in targets:
        stock_code = target["stock_code"]
        stock_name = target["stock_name"] or stock_code
        model_id = target["model_id"]
        if not args.force and await has_successful_cached_analysis(
            settings.db_path,
            stock_code=stock_code,
            trade_date_value=trade_date_value,
            model_id=model_id,
        ):
            skipped_count += 1
            logger.info("Skip %s (%s) with %s: success cache already exists for %s", stock_code, stock_name, model_id, trade_date_value)
            continue
        logger.info("Generating shared analysis for %s (%s) with %s", stock_code, stock_name, model_id)
        result = _run_target_with_timeout(settings, target, trade_date_value)
        result_trade_date = date.fromisoformat(result["trade_date"])
        if result["status"] == "success":
            if not args.dry_run:
                market_payload = json.loads(result["market_data_json"])
                await upsert_analysis_cache(
                    settings.db_path,
                    stock_code=stock_code,
                    stock_name=str(market_payload.get("stock_name", stock_name)),
                    trade_date_value=result_trade_date,
                    model_id=model_id,
                    status="success",
                    error_message="",
                    market_data_json=result["market_data_json"],
                    analysis_json=result["analysis_json"],
                    news_json=result["news_json"],
                    actual_provider=result["actual_provider"],
                    actual_model_name=result["actual_model_name"],
                    provider_response_id=result["provider_response_id"],
                )
            success_count += 1
        else:
            logger.error("Shared analysis failed for %s/%s: %s", stock_code, model_id, result["error_message"])
            failure_count += 1
            if not args.dry_run:
                await upsert_analysis_cache(
                    settings.db_path,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    trade_date_value=result_trade_date,
                    model_id=model_id,
                    status="failed",
                    error_message=result["error_message"],
                    market_data_json=result["market_data_json"],
                    analysis_json=result["analysis_json"],
                    news_json=result["news_json"],
                    actual_provider=result["actual_provider"],
                    actual_model_name=result["actual_model_name"],
                    provider_response_id=result["provider_response_id"],
                )

    logger.info("Shared generation completed: success=%s failure=%s skipped=%s", success_count, failure_count, skipped_count)
    return 0 if failure_count == 0 else 1


def main() -> int:
    return __import__("asyncio").run(run())


if __name__ == "__main__":
    sys.exit(main())
