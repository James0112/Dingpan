from __future__ import annotations

import json

from src.providers import GenerateConfig, get_provider
from src.schemas import AnalysisResult, MarketData, NewsItem


class AnalysisError(RuntimeError):
    pass


def _build_prompt(market_data: MarketData, news_list: list[NewsItem]) -> str:
    news_block = "近期无重大新闻"
    if news_list:
        news_lines = []
        for item in news_list:
            news_lines.append(
                f"- [{item.published_at.strftime('%Y-%m-%d %H:%M')}] {item.title}\n  摘要：{item.summary}"
            )
        news_block = "\n".join(news_lines)

    return f"""你是一位专业的A股短线分析师。以下是{market_data.stock_name}({market_data.stock_code})的【真实行情数据】，
所有数字均来自东方财富，请严格基于这些数据分析，不要编造任何价格或时间。

## 基本信息
- 股票：{market_data.stock_name} {market_data.stock_code}
- 最新交易日：{market_data.latest_trade_date}

## 当日行情
- 开盘：{market_data.snapshot.open_price:.2f} | 最高：{market_data.snapshot.high_price:.2f} | 最低：{market_data.snapshot.low_price:.2f} | 收盘：{market_data.snapshot.close_price:.2f}
- 涨跌幅：{market_data.snapshot.change_pct:+.2f}%
- 成交量：{int(market_data.snapshot.volume)}手 | 成交额：{market_data.snapshot.amount:.2f}万元
- 相比5日均量：{market_data.indicators.volume_trend}

## 技术指标
- MA5：{market_data.indicators.ma5:.2f} | MA10：{market_data.indicators.ma10:.2f} | MA20：{market_data.indicators.ma20:.2f}
- 当前价格位于均线：{market_data.indicators.price_vs_ma}
- MACD：DIF {market_data.indicators.dif:.4f}, DEA {market_data.indicators.dea:.4f}, 柱 {market_data.indicators.macd_hist:.4f}
- MACD状态：{market_data.indicators.macd_status}

## 资金流向（当日）
- 主力净流入：{market_data.fund_flow.main_net_inflow:.2f}万元
- 超大单：{market_data.fund_flow.xl_net_inflow:.2f}万 | 大单：{market_data.fund_flow.large_net_inflow:.2f}万
- 中单：{market_data.fund_flow.medium_net_inflow:.2f}万 | 小单：{market_data.fund_flow.small_net_inflow:.2f}万

## 近5个交易日走势摘要
{market_data.recent_5d_summary}

## 近期相关新闻（来自东方财富，已按时间筛选）
{news_block}

## 输出要求
请严格返回以下 JSON 格式，不要包含 markdown 代码块标记，不要有任何前导或后缀文字：
{{
  "executive_summary": "2-3句话总结整体判断，不重复具体数字",
  "market_review": "3-4句话复盘当日盘面节奏，可写具体价格和走势细节",
  "technical_signals": ["信号1", "信号2", "信号3"],
  "technical_analysis": "1段3-5句解释均线结构、MACD阶段、量价关系和近5日趋势",
  "fund_flow_analysis": "2-3句话解读主力资金意图",
  "news_impact": "2-3句话总结近期新闻对股价的潜在影响；如果新闻为空，输出近期无重大新闻",
  "news_sentiment": "positive 或 negative 或 neutral",
  "action_advice": "面向已有持仓者的操作建议，可补一句未持仓者是否适合追高或等待确认",
  "risk_notes": ["风险1", "风险2"],
  "bias": "bullish 或 bearish 或 neutral",
  "support_price": 10.20,
  "resistance_price": 10.95
}}

## 约束规则（必须遵守）
1. 若数据不足以支持明确方向，bias 输出 "neutral"，建议输出"观望"
2. support_price/resistance_price 必须输出纯数字，基于均线、近5日高低点或收盘价附近推导，不要虚构远端价位，也不要输出“10.2附近”这类文字
3. 不要使用"必涨""必跌""大概率涨停"等确定性表达
4. 操作建议使用稳健措辞：持有观察、逢高减仓、不追高、若回踩XX企稳可考虑低吸、观望等待
5. technical_signals 数组长度 2-4 条
6. risk_notes 数组长度 1-3 条，每条简洁明确，不要写成长段
7. executive_summary 负责整体判断，不重复具体数字；market_review 负责盘面节奏，可写具体价格和走势细节
8. 新闻解读必须基于给定新闻列表，不要编造不存在的事件、政策、财报或产业逻辑
9. 不要假设用户的仓位比例、现金比例、摊薄成本或加仓历史
10. 这是股票级共享分析，不要引用任何特定持仓成本或个人交易背景
11. 只返回 JSON，不要包含任何其他文字
"""


def _fallback_price(market_data: MarketData, field_name: str) -> float:
    if field_name == "support_price":
        return min(market_data.indicators.ma20, market_data.snapshot.low_price)
    return max(market_data.indicators.ma5, market_data.snapshot.high_price)


def _coerce_price(payload: dict[str, object], field_name: str, market_data: MarketData) -> float:
    raw_value = payload.get(field_name)
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return _fallback_price(market_data, field_name)


def _parse_response(raw_text: str, market_data: MarketData) -> AnalysisResult:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"Model returned invalid JSON: {exc}") from exc

    required = {
        "executive_summary",
        "market_review",
        "technical_signals",
        "technical_analysis",
        "fund_flow_analysis",
        "news_impact",
        "news_sentiment",
        "action_advice",
        "risk_notes",
        "bias",
        "support_price",
        "resistance_price",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise AnalysisError(f"Missing analysis fields: {missing}")

    technical_signals = payload["technical_signals"]
    if not isinstance(technical_signals, list) or not 2 <= len(technical_signals) <= 4:
        raise AnalysisError("technical_signals must be a list with 2-4 items")
    risk_notes = payload["risk_notes"]
    if not isinstance(risk_notes, list) or not 1 <= len(risk_notes) <= 3:
        raise AnalysisError("risk_notes must be a list with 1-3 items")
    bias = str(payload["bias"]).strip().lower()
    news_sentiment = str(payload["news_sentiment"]).strip().lower()
    if bias not in {"bullish", "bearish", "neutral"}:
        raise AnalysisError(f"Invalid bias value: {bias}")
    if news_sentiment not in {"positive", "negative", "neutral"}:
        raise AnalysisError(f"Invalid news_sentiment value: {news_sentiment}")

    return AnalysisResult(
        executive_summary=str(payload["executive_summary"]).strip(),
        market_review=str(payload["market_review"]).strip(),
        technical_signals=[str(item).strip() for item in technical_signals],
        technical_analysis=str(payload["technical_analysis"]).strip(),
        fund_flow_analysis=str(payload["fund_flow_analysis"]).strip(),
        news_impact=str(payload["news_impact"]).strip(),
        news_sentiment=news_sentiment,
        action_advice=str(payload["action_advice"]).strip(),
        risk_notes=[str(item).strip() for item in risk_notes],
        bias=bias,
        support_price=_coerce_price(payload, "support_price", market_data),
        resistance_price=_coerce_price(payload, "resistance_price", market_data),
    )


def analyze_market_data(
    api_key: str,
    model_id: str,
    model_name: str,
    market_data: MarketData,
    news_list: list[NewsItem],
    fallback_model_name: str | None = None,
) -> AnalysisResult:
    if not api_key:
        raise AnalysisError("GEMINI_API_KEY is required")

    prompt = _build_prompt(market_data, news_list)
    provider = get_provider(
        model_id,
        api_key=api_key,
        model_name=model_name,
        fallback_model_name=fallback_model_name,
    )
    try:
        raw_text = provider.generate(prompt, GenerateConfig())
    except Exception as exc:  # pragma: no cover - provider/runtime dependent
        raise AnalysisError(f"{model_id} analysis failed: {exc}") from exc
    return _parse_response(raw_text, market_data)
