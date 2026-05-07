from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime

from src.cost_engine import generate_cost_analysis
from src.schemas import AnalysisResult, FundFlow, MarketData, MarketSnapshot, NewsItem, TechnicalIndicators


ANALYSIS_VERSION = 1


def market_data_to_json(market_data: MarketData) -> str:
    payload = asdict(market_data)
    payload["latest_trade_date"] = market_data.latest_trade_date.isoformat()
    payload["snapshot"]["trade_date"] = market_data.snapshot.trade_date.isoformat()
    return json.dumps(payload, ensure_ascii=False)


def analysis_result_to_json(analysis: AnalysisResult) -> str:
    return json.dumps(asdict(analysis), ensure_ascii=False)


def news_list_to_json(news_list: list[NewsItem]) -> str:
    payload = [
        {
            "published_at": item.published_at.isoformat(),
            "title": item.title,
            "summary": item.summary,
            "url": item.url,
        }
        for item in news_list
    ]
    return json.dumps(payload, ensure_ascii=False)


def market_data_from_json(raw: str) -> MarketData:
    payload = json.loads(raw)
    snapshot = payload["snapshot"]
    indicators = payload["indicators"]
    fund_flow = payload["fund_flow"]
    return MarketData(
        stock_code=str(payload["stock_code"]),
        stock_name=str(payload["stock_name"]),
        cost_price=float(payload.get("cost_price", 0.0)),
        latest_trade_date=date.fromisoformat(payload["latest_trade_date"]),
        snapshot=MarketSnapshot(
            trade_date=date.fromisoformat(snapshot["trade_date"]),
            open_price=float(snapshot["open_price"]),
            high_price=float(snapshot["high_price"]),
            low_price=float(snapshot["low_price"]),
            close_price=float(snapshot["close_price"]),
            volume=float(snapshot["volume"]),
            amount=float(snapshot["amount"]),
            change_pct=float(snapshot["change_pct"]),
        ),
        indicators=TechnicalIndicators(
            ma5=float(indicators["ma5"]),
            ma10=float(indicators["ma10"]),
            ma20=float(indicators["ma20"]),
            volume_ma5=float(indicators["volume_ma5"]),
            volume_ratio=float(indicators["volume_ratio"]),
            volume_trend=str(indicators["volume_trend"]),
            price_vs_ma=str(indicators["price_vs_ma"]),
            dif=float(indicators["dif"]),
            dea=float(indicators["dea"]),
            macd_hist=float(indicators["macd_hist"]),
            macd_status=str(indicators["macd_status"]),
        ),
        fund_flow=FundFlow(
            main_net_inflow=float(fund_flow["main_net_inflow"]),
            xl_net_inflow=float(fund_flow["xl_net_inflow"]),
            large_net_inflow=float(fund_flow["large_net_inflow"]),
            medium_net_inflow=float(fund_flow["medium_net_inflow"]),
            small_net_inflow=float(fund_flow["small_net_inflow"]),
        ),
        recent_5d_summary=str(payload["recent_5d_summary"]),
    )


def analysis_result_from_json(raw: str) -> AnalysisResult:
    payload = json.loads(raw)
    return AnalysisResult(
        executive_summary=str(payload["executive_summary"]),
        market_review=str(payload["market_review"]),
        technical_signals=[str(item) for item in payload["technical_signals"]],
        technical_analysis=str(payload["technical_analysis"]),
        fund_flow_analysis=str(payload["fund_flow_analysis"]),
        news_impact=str(payload["news_impact"]),
        news_sentiment=str(payload["news_sentiment"]),
        action_advice=str(payload["action_advice"]),
        risk_notes=[str(item) for item in payload["risk_notes"]],
        bias=str(payload["bias"]),
        support_price=float(payload["support_price"]),
        resistance_price=float(payload["resistance_price"]),
    )


def news_list_from_json(raw: str) -> list[NewsItem]:
    payload = json.loads(raw)
    return [
        NewsItem(
            published_at=datetime.fromisoformat(item["published_at"]),
            title=str(item["title"]),
            summary=str(item["summary"]),
            url=str(item["url"]),
        )
        for item in payload
    ]


def build_report_context(
    market_data: MarketData,
    analysis: AnalysisResult,
    news_list: list[NewsItem],
    *,
    cost_price: float,
    model_id: str,
    previous_trade_date: str | None,
    next_trade_date: str | None,
) -> dict[str, object]:
    adjusted_market_data = MarketData(
        stock_code=market_data.stock_code,
        stock_name=market_data.stock_name,
        cost_price=cost_price,
        latest_trade_date=market_data.latest_trade_date,
        snapshot=market_data.snapshot,
        indicators=market_data.indicators,
        fund_flow=market_data.fund_flow,
        recent_5d_summary=market_data.recent_5d_summary,
    )
    cost_analysis = generate_cost_analysis(
        close_price=market_data.snapshot.close_price,
        cost_price=cost_price,
        support_price=analysis.support_price,
        resistance_price=analysis.resistance_price,
        ma5=market_data.indicators.ma5,
        ma10=market_data.indicators.ma10,
        ma20=market_data.indicators.ma20,
        bias=analysis.bias,
    )
    pnl_pct = cost_analysis.pnl_pct if cost_price > 0 else None
    return {
        "market_data": adjusted_market_data,
        "analysis": analysis,
        "news_list": news_list,
        "cost_analysis": cost_analysis,
        "model_id": model_id,
        "previous_trade_date": previous_trade_date,
        "next_trade_date": next_trade_date,
        "pnl_pct": pnl_pct,
    }
