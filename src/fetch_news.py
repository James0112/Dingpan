from __future__ import annotations

import re
from datetime import date, datetime
from difflib import SequenceMatcher

import akshare as ak
import pandas as pd

from src.schemas import NewsItem


def _normalize_title(title: str) -> str:
    text = re.sub(r"\s+", "", title)
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text).lower()


def _is_similar_title(title: str, existing: list[str], threshold: float = 0.8) -> bool:
    normalized = _normalize_title(title)
    for other in existing:
        if SequenceMatcher(None, normalized, other).ratio() >= threshold:
            return True
    return False


def fetch_news(stock_code: str, latest_trade_date: date, lookback_hours: int, max_items: int) -> list[NewsItem]:
    try:
        news_df = ak.stock_news_em(symbol=stock_code)
    except Exception:
        return []
    if news_df.empty:
        return []

    columns_map = {
        "新闻标题": "title",
        "新闻内容": "summary",
        "发布时间": "published_at",
        "新闻链接": "url",
    }
    missing = [column for column in columns_map if column not in news_df.columns]
    if missing:
        return []

    df = news_df[list(columns_map)].rename(columns=columns_map).copy()
    df["published_at"] = pd.to_datetime(df["published_at"], errors="coerce")
    df = df.dropna(subset=["published_at", "title"])

    window_start = pd.Timestamp(latest_trade_date) - pd.Timedelta(hours=lookback_hours)
    window_end = pd.Timestamp(latest_trade_date) + pd.Timedelta(days=1, hours=12)
    df = df[(df["published_at"] >= window_start) & (df["published_at"] <= window_end)]
    df = df.sort_values("published_at", ascending=False)

    selected: list[NewsItem] = []
    normalized_titles: list[str] = []
    for _, row in df.iterrows():
        title = str(row["title"]).strip()
        if not title or _is_similar_title(title, normalized_titles):
            continue
        normalized_titles.append(_normalize_title(title))
        summary = str(row["summary"]).strip().replace("\n", " ")
        selected.append(
            NewsItem(
                published_at=row["published_at"].to_pydatetime(),
                title=title,
                summary=summary[:100] if summary else "暂无摘要",
                url=str(row["url"]).strip(),
            )
        )
        if len(selected) >= max_items:
            break
    return selected
