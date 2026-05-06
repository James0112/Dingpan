from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class MarketSnapshot:
    trade_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    amount: float
    change_pct: float


@dataclass(frozen=True)
class TechnicalIndicators:
    ma5: float
    ma10: float
    ma20: float
    volume_ma5: float
    volume_ratio: float
    volume_trend: str
    price_vs_ma: str
    dif: float
    dea: float
    macd_hist: float
    macd_status: str


@dataclass(frozen=True)
class FundFlow:
    main_net_inflow: float
    xl_net_inflow: float
    large_net_inflow: float
    medium_net_inflow: float
    small_net_inflow: float


@dataclass(frozen=True)
class NewsItem:
    published_at: datetime
    title: str
    summary: str
    url: str


@dataclass(frozen=True)
class MarketData:
    stock_code: str
    stock_name: str
    cost_price: float
    latest_trade_date: date
    snapshot: MarketSnapshot
    indicators: TechnicalIndicators
    fund_flow: FundFlow
    recent_5d_summary: str


@dataclass(frozen=True)
class AnalysisResult:
    executive_summary: str
    market_review: str
    technical_signals: list[str]
    technical_analysis: str
    fund_flow_analysis: str
    news_impact: str
    news_sentiment: str
    cost_analysis: str
    action_advice: str
    risk_notes: list[str]
    bias: str
    support_price: str
    resistance_price: str
