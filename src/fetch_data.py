from __future__ import annotations

from datetime import date, timedelta

import akshare as ak
import pandas as pd

from src.schemas import FundFlow, MarketData, MarketSnapshot, TechnicalIndicators


class DataFetchError(RuntimeError):
    pass


def _market_prefix(stock_code: str) -> str:
    if stock_code.startswith(("600", "601", "603", "605", "688", "900")):
        return "sh"
    return "sz"


def _resolve_stock_name(stock_code: str, preferred_name: str) -> str:
    if preferred_name.strip():
        return preferred_name.strip()
    return stock_code


def _safe_float(value: object) -> float:
    return float(str(value).replace(",", ""))


def _load_hist_df(stock_code: str, latest_trade_date: date) -> pd.DataFrame:
    start_date = (latest_trade_date - timedelta(days=120)).strftime("%Y%m%d")
    end_date = latest_trade_date.strftime("%Y%m%d")
    prefixed_symbol = f"{_market_prefix(stock_code)}{stock_code}"
    attempts: list[tuple[str, object]] = [
        (
            "eastmoney",
            lambda: ak.stock_zh_a_hist(
                symbol=stock_code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            ),
        ),
        (
            "sina",
            lambda: ak.stock_zh_a_daily(
                symbol=prefixed_symbol,
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            ),
        ),
    ]

    errors: list[str] = []
    for source_name, loader in attempts:
        try:
            hist_df = loader()
            if hist_df is not None and not hist_df.empty:
                hist_df.attrs["source"] = source_name
                return hist_df.copy()
            errors.append(f"{source_name}: empty data")
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            errors.append(f"{source_name}: {exc}")
    raise DataFetchError(f"Failed to fetch stock history from all providers: {' | '.join(errors)}")


def _normalize_hist_df(hist_df: pd.DataFrame) -> pd.DataFrame:
    df = hist_df.copy()
    source = hist_df.attrs.get("source", "unknown")
    if source == "sina":
        df = df.reset_index()
        rename_map = {
            "date": "日期",
            "open": "开盘",
            "close": "收盘",
            "high": "最高",
            "low": "最低",
            "volume": "成交量",
            "amount": "成交额",
        }
        df = df.rename(columns=rename_map)
        if "涨跌幅" not in df.columns:
            df["涨跌幅"] = df["收盘"].pct_change() * 100

    required_columns = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "涨跌幅"]
    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        raise DataFetchError(f"Missing history columns from {source}: {missing}")

    df = df[required_columns].copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    for column in required_columns[1:]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if source == "sina":
        df["涨跌幅"] = df["涨跌幅"].fillna(0.0)

    if df[required_columns].isnull().any().any():
        raise DataFetchError(f"Stock history from {source} contains null values in required columns")

    df = df.sort_values("日期").reset_index(drop=True)
    df["MA5"] = df["收盘"].rolling(5).mean()
    df["MA10"] = df["收盘"].rolling(10).mean()
    df["MA20"] = df["收盘"].rolling(20).mean()
    df["VOL_MA5"] = df["成交量"].rolling(5).mean()
    ema12 = df["收盘"].ewm(span=12, adjust=False).mean()
    ema26 = df["收盘"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = (df["DIF"] - df["DEA"]) * 2
    return df


def _validate_row(row: pd.Series) -> None:
    high_price = _safe_float(row["最高"])
    low_price = _safe_float(row["最低"])
    open_price = _safe_float(row["开盘"])
    close_price = _safe_float(row["收盘"])
    change_pct = _safe_float(row["涨跌幅"])

    if high_price < low_price:
        raise DataFetchError("Invalid price range: high < low")
    if not low_price <= close_price <= high_price:
        raise DataFetchError("Invalid close price range")
    if not low_price <= open_price <= high_price:
        raise DataFetchError("Invalid open price range")
    if abs(change_pct) > 20:
        raise DataFetchError("Change percentage is outside expected A-share bounds")


def _price_vs_ma(close_price: float, ma5: float, ma10: float, ma20: float) -> str:
    positions = []
    for label, value in (("MA5", ma5), ("MA10", ma10), ("MA20", ma20)):
        positions.append(f"高于{label}" if close_price >= value else f"低于{label}")
    return "，".join(positions)


def _volume_trend(volume: float, volume_ma5: float) -> tuple[str, float]:
    ratio = volume / volume_ma5 if volume_ma5 else 1.0
    if ratio > 1.05:
        return "放量", ratio
    if ratio < 0.95:
        return "缩量", ratio
    return "平量", ratio


def _macd_status(df: pd.DataFrame) -> str:
    latest = df.iloc[-1]
    previous = df.iloc[-2] if len(df) >= 2 else latest
    if latest["DIF"] > latest["DEA"] and previous["DIF"] <= previous["DEA"]:
        return "金叉"
    if latest["DIF"] < latest["DEA"] and previous["DIF"] >= previous["DEA"]:
        return "死叉"
    return "多头" if latest["DIF"] >= latest["DEA"] else "空头"


def _recent_5d_summary(df: pd.DataFrame) -> str:
    latest_rows = df.tail(5)
    lines = []
    for _, row in latest_rows.iterrows():
        trade_day = row["日期"].strftime("%m-%d")
        lines.append(
            f"{trade_day} 收{row['收盘']:.2f}元，涨跌幅{row['涨跌幅']:+.2f}%，成交量{int(row['成交量'])}手"
        )
    return "；".join(lines)


def _load_fund_flow(stock_code: str, latest_trade_date: date) -> FundFlow:
    market = _market_prefix(stock_code)
    try:
        fund_df = ak.stock_individual_fund_flow(stock=stock_code, market=market)
    except TypeError:
        try:
            fund_df = ak.stock_individual_fund_flow(symbol=stock_code)
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            raise DataFetchError(f"Failed to fetch fund flow: {exc}") from exc
    except Exception as exc:  # pragma: no cover - network/runtime dependent
        raise DataFetchError(f"Failed to fetch fund flow: {exc}") from exc

    if fund_df.empty:
        raise DataFetchError("Fund flow data is empty")

    df = fund_df.copy()
    if "日期" in df.columns:
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        matched = df[df["日期"] == pd.Timestamp(latest_trade_date)]
        if not matched.empty:
            df = matched
    latest_row = df.iloc[-1]

    def field(*candidates: str) -> float:
        for column in candidates:
            if column in latest_row.index:
                return _safe_float(latest_row[column]) / 10000
        raise DataFetchError(f"Missing fund flow column candidates: {candidates}")

    return FundFlow(
        main_net_inflow=field("主力净流入-净额", "主力净流入"),
        xl_net_inflow=field("超大单净流入-净额", "超大单净流入"),
        large_net_inflow=field("大单净流入-净额", "大单净流入"),
        medium_net_inflow=field("中单净流入-净额", "中单净流入"),
        small_net_inflow=field("小单净流入-净额", "小单净流入"),
    )


def fetch_market_data(stock_code: str, stock_name: str, cost_price: float, latest_trade_date: date) -> MarketData:
    hist_df = _normalize_hist_df(_load_hist_df(stock_code, latest_trade_date))
    matched = hist_df[hist_df["日期"] == pd.Timestamp(latest_trade_date)]
    latest_row = matched.iloc[-1] if not matched.empty else hist_df.iloc[-1]
    actual_trade_date = latest_row["日期"].date()
    _validate_row(latest_row)

    required_indicator_columns = ["MA5", "MA10", "MA20", "VOL_MA5", "DIF", "DEA", "MACD_HIST"]
    if latest_row[required_indicator_columns].isnull().any():
        raise DataFetchError("Insufficient history to compute indicators")

    volume_trend, volume_ratio = _volume_trend(_safe_float(latest_row["成交量"]), _safe_float(latest_row["VOL_MA5"]))
    snapshot = MarketSnapshot(
        trade_date=actual_trade_date,
        open_price=_safe_float(latest_row["开盘"]),
        high_price=_safe_float(latest_row["最高"]),
        low_price=_safe_float(latest_row["最低"]),
        close_price=_safe_float(latest_row["收盘"]),
        volume=_safe_float(latest_row["成交量"]),
        amount=_safe_float(latest_row["成交额"]) / 10000,
        change_pct=_safe_float(latest_row["涨跌幅"]),
    )
    indicators = TechnicalIndicators(
        ma5=_safe_float(latest_row["MA5"]),
        ma10=_safe_float(latest_row["MA10"]),
        ma20=_safe_float(latest_row["MA20"]),
        volume_ma5=_safe_float(latest_row["VOL_MA5"]),
        volume_ratio=volume_ratio,
        volume_trend=volume_trend,
        price_vs_ma=_price_vs_ma(
            snapshot.close_price,
            _safe_float(latest_row["MA5"]),
            _safe_float(latest_row["MA10"]),
            _safe_float(latest_row["MA20"]),
        ),
        dif=_safe_float(latest_row["DIF"]),
        dea=_safe_float(latest_row["DEA"]),
        macd_hist=_safe_float(latest_row["MACD_HIST"]),
        macd_status=_macd_status(hist_df),
    )
    fund_flow = _load_fund_flow(stock_code, actual_trade_date)
    return MarketData(
        stock_code=stock_code,
        stock_name=_resolve_stock_name(stock_code, stock_name),
        cost_price=cost_price,
        latest_trade_date=actual_trade_date,
        snapshot=snapshot,
        indicators=indicators,
        fund_flow=fund_flow,
        recent_5d_summary=_recent_5d_summary(hist_df),
    )
