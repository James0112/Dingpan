from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd


class TradingCalendarError(RuntimeError):
    pass


def get_today(tz_name: str) -> date:
    return pd.Timestamp.now(tz=ZoneInfo(tz_name)).date()


def load_trade_calendar() -> pd.DatetimeIndex:
    try:
        calendar_df = ak.tool_trade_date_hist_sina()
    except Exception as exc:  # pragma: no cover - network/runtime dependent
        raise TradingCalendarError(f"Failed to load trade calendar: {exc}") from exc
    if "trade_date" not in calendar_df.columns:
        raise TradingCalendarError("trade_date column missing from trade calendar")
    trade_dates = pd.to_datetime(calendar_df["trade_date"]).sort_values().unique()
    return pd.DatetimeIndex(trade_dates)


def is_today_trading_day(today: date, trade_dates: pd.DatetimeIndex) -> bool:
    today_ts = pd.Timestamp(today)
    if len(trade_dates) == 0:
        raise TradingCalendarError("Trade calendar is empty")
    if today_ts > trade_dates.max():
        return today.weekday() < 5
    return today_ts in trade_dates


def get_latest_trade_date(today: date, trade_dates: pd.DatetimeIndex) -> date:
    today_ts = pd.Timestamp(today)
    eligible = trade_dates[trade_dates < today_ts]
    if len(eligible) == 0:
        raise TradingCalendarError("No previous trade date found before today")
    return eligible[-1].date()


def fallback_latest_trade_date(today: date) -> date:
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def is_calendar_stale(today: date, trade_dates: pd.DatetimeIndex, max_lag_days: int = 3) -> bool:
    if len(trade_dates) == 0:
        return True
    latest_known = trade_dates.max().date()
    return (today - latest_known).days > max_lag_days
