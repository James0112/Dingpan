from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.schemas import AnalysisResult, CostAnalysis, MarketData, NewsItem


logger = logging.getLogger("dingpan")


def _change_style(change_pct: float) -> tuple[str, str]:
    if change_pct > 0:
        return "#ff4757", "▲"
    if change_pct < 0:
        return "#2ed573", "▼"
    return "#a0a0a0", "━"


def _bias_style(bias: str) -> tuple[str, str, str]:
    if bias == "bullish":
        return "#2ed573", "#0d2818", "偏多 · 持有观察"
    if bias == "bearish":
        return "#ff4757", "#2d0f14", "偏空 · 控制仓位"
    return "#ffa502", "#2d2310", "震荡 · 观望等待"


def _news_style(sentiment: str) -> tuple[str, str]:
    if sentiment == "positive":
        return "#ff4757", "利好"
    if sentiment == "negative":
        return "#2ed573", "利空"
    return "#a0a0a0", "中性"


def _format_signed_percent(value: float) -> str:
    if value > 0:
        return f"+{value:.2f}"
    return f"{value:.2f}"


def _format_cost_block(cost_price: float, close_price: float) -> tuple[str, str, str]:
    if cost_price <= 0:
        return "未配置", "#a0a0a0", "未配置"
    pnl_pct = ((close_price - cost_price) / cost_price) * 100
    pnl_color, _ = _change_style(pnl_pct)
    return f"{cost_price:.2f}", pnl_color, f"{_format_signed_percent(pnl_pct)}%"


def build_subject(market_data: MarketData) -> str:
    color, arrow = _change_style(market_data.snapshot.change_pct)
    del color
    return (
        f"盯盘侠 | {market_data.stock_name} {arrow}{_format_signed_percent(market_data.snapshot.change_pct)}% "
        f"收{market_data.snapshot.close_price:.2f} | {market_data.latest_trade_date:%m-%d}"
    )


def render_email(
    template_path: Path,
    output_dir: Path,
    market_data: MarketData,
    analysis: AnalysisResult,
    cost_analysis: CostAnalysis,
    news_list: list[NewsItem],
    generated_at: datetime,
) -> tuple[str, Path, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_path.name)

    change_bg_color, change_arrow = _change_style(market_data.snapshot.change_pct)
    cost_price_display, pnl_color, pnl_pct_display = _format_cost_block(
        market_data.cost_price,
        market_data.snapshot.close_price,
    )
    advice_border_color, advice_bg, advice_direction = _bias_style(analysis.bias)
    news_badge_color, news_badge_text = _news_style(analysis.news_sentiment)
    subject = build_subject(market_data)

    html = template.render(
        date=f"{market_data.latest_trade_date:%Y-%m-%d}",
        stock_name=market_data.stock_name,
        stock_code=market_data.stock_code,
        close_price=f"{market_data.snapshot.close_price:.2f}",
        open_price=f"{market_data.snapshot.open_price:.2f}",
        high_price=f"{market_data.snapshot.high_price:.2f}",
        low_price=f"{market_data.snapshot.low_price:.2f}",
        amount=f"{market_data.snapshot.amount:,.0f}",
        change_bg_color=change_bg_color,
        change_arrow=change_arrow,
        change_pct=_format_signed_percent(market_data.snapshot.change_pct),
        cost_price=cost_price_display,
        pnl_pct=pnl_pct_display,
        pnl_color=pnl_color,
        executive_summary=analysis.executive_summary,
        section_review=analysis.market_review,
        technical_signals=analysis.technical_signals,
        technical_analysis=analysis.technical_analysis,
        main_flow=f"{market_data.fund_flow.main_net_inflow:,.0f}",
        main_flow_color=_change_style(market_data.fund_flow.main_net_inflow)[0],
        xl_flow=f"{market_data.fund_flow.xl_net_inflow:,.0f}",
        xl_flow_color=_change_style(market_data.fund_flow.xl_net_inflow)[0],
        sm_flow=f"{market_data.fund_flow.small_net_inflow:,.0f}",
        sm_flow_color=_change_style(market_data.fund_flow.small_net_inflow)[0],
        section_fund_flow=analysis.fund_flow_analysis,
        news_list=news_list,
        news_badge_color=news_badge_color,
        news_badge_text=news_badge_text,
        news_impact=analysis.news_impact,
        cost_analysis=cost_analysis.cost_position_analysis,
        cost_advice=cost_analysis.cost_advice,
        advice_bg=advice_bg,
        advice_border_color=advice_border_color,
        advice_direction=advice_direction,
        section_advice=analysis.action_advice,
        risk_notes=analysis.risk_notes,
        support_price=f"{analysis.support_price:.2f}",
        resistance_price=f"{analysis.resistance_price:.2f}",
        generated_at=generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )

    html_path = output_dir / f"dingpan_report_{market_data.latest_trade_date:%Y%m%d}_{market_data.stock_code}.html"
    try:
        html_path.write_text(html, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to write rendered HTML artifact %s: %s", html_path, exc)
    return subject, html_path, html
