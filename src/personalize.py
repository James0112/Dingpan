from __future__ import annotations

import json
from dataclasses import dataclass

from src.config import Settings
from src.providers import GenerateConfig, get_provider
from src.schemas import AnalysisResult, MarketData, PersonalizedAnalysisResult, UserProfile


class PersonalizedAnalysisError(RuntimeError):
    pass


@dataclass(frozen=True)
class PersonalizedOutput:
    result: PersonalizedAnalysisResult
    actual_provider: str
    actual_model_name: str
    provider_response_id: str


def _serialize_profile(profile: UserProfile) -> str:
    lines = [
        f"- 风险偏好：{profile.risk_preference or '未填写'}",
        f"- 交易风格：{profile.trading_style or '未填写'}",
        f"- 关注板块：{', '.join(profile.focus_sectors) if profile.focus_sectors else '未填写'}",
        f"- 持仓备注：{profile.position_notes or '未填写'}",
        f"- 其他补充：{profile.custom_notes or '未填写'}",
    ]
    return "\n".join(lines)


def _build_prompt(market_data: MarketData, shared_analysis: AnalysisResult, profile: UserProfile) -> str:
    holding_line = "未填写持仓成本"
    if market_data.cost_price > 0:
        pnl_pct = ((market_data.snapshot.close_price - market_data.cost_price) / market_data.cost_price) * 100
        holding_line = (
            f"持仓成本 {market_data.cost_price:.2f}，现价 {market_data.snapshot.close_price:.2f}，"
            f"浮盈亏 {pnl_pct:+.2f}%"
        )

    return f"""你是一位A股盘后复盘助手。现在你不需要重复做盘面分析，只需要基于已有共享分析结论，
结合这位用户的持仓背景与手动维护画像，输出个性化建议。

## 股票与持仓
- 股票：{market_data.stock_name} {market_data.stock_code}
- 交易日：{market_data.latest_trade_date}
- 用户持仓：{holding_line}

## 用户画像
{_serialize_profile(profile)}

## 共享分析结论
- 共享摘要：{shared_analysis.general_summary}
- 市场方向：{shared_analysis.bias}
- 技术信号：{'；'.join(shared_analysis.technical_signals)}
- 技术分析：{shared_analysis.technical_analysis}
- 资金解读：{shared_analysis.fund_flow_analysis}
- 新闻影响：{shared_analysis.news_impact}
- 共享风险：{'；'.join(shared_analysis.risk_notes)}
- 支撑位：{shared_analysis.support_price:.2f}
- 压力位：{shared_analysis.resistance_price:.2f}

## 输出要求
只返回 JSON，不要 markdown，不要解释文字：
{{
  "executive_summary": "2-3句话，站在该用户当前持仓与风格角度，重写核心结论",
  "action_advice": "给这位用户的具体操作建议，允许引用成本位、支撑位、压力位，但不要夸张确定性",
  "personal_risk_notes": ["风险1", "风险2"]
}}

## 约束
1. 只能基于给定共享分析和画像，不得虚构消息、仓位比例、交易历史
2. 若用户未填写画像，按普通持仓用户输出，不要假装知道更多背景
3. 若未填写持仓成本，不要编造成本文本中不存在的成本线
4. `personal_risk_notes` 长度必须为 1-3 条，每条简洁明确
5. 不要重复大段共享分析原文，重点体现“这位用户该怎么理解和处理”
6. 使用稳健措辞，避免必然性表达
"""


def _parse_response(raw_text: str) -> PersonalizedAnalysisResult:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PersonalizedAnalysisError(f"Model returned invalid personalized JSON: {exc}") from exc

    required = {"executive_summary", "action_advice", "personal_risk_notes"}
    missing = sorted(required - payload.keys())
    if missing:
        raise PersonalizedAnalysisError(f"Missing personalized fields: {missing}")

    notes = payload["personal_risk_notes"]
    if not isinstance(notes, list) or not 1 <= len(notes) <= 3:
        raise PersonalizedAnalysisError("personal_risk_notes must be a list with 1-3 items")

    return PersonalizedAnalysisResult(
        executive_summary=str(payload["executive_summary"]).strip(),
        action_advice=str(payload["action_advice"]).strip(),
        personal_risk_notes=[str(item).strip() for item in notes],
    )


def generate_personalized_analysis(
    model_id: str,
    market_data: MarketData,
    shared_analysis: AnalysisResult,
    profile: UserProfile,
    *,
    db_path: str,
    settings: Settings,
) -> PersonalizedOutput:
    prompt = _build_prompt(market_data, shared_analysis, profile)
    provider = get_provider(db_path, settings, model_id)
    last_error: PersonalizedAnalysisError | None = None
    for attempt in range(3):
        try:
            provider_result = provider.generate(prompt, GenerateConfig(max_output_tokens=1200))
        except Exception as exc:  # pragma: no cover - provider/runtime dependent
            raise PersonalizedAnalysisError(f"{model_id} personalized analysis failed: {exc}") from exc
        try:
            return PersonalizedOutput(
                result=_parse_response(provider_result.text),
                actual_provider=provider_result.actual_provider,
                actual_model_name=provider_result.actual_model_name,
                provider_response_id=provider_result.provider_response_id,
            )
        except PersonalizedAnalysisError as exc:
            last_error = exc
            if attempt == 2:
                break
    if last_error is not None:
        raise last_error
    raise PersonalizedAnalysisError(f"{model_id} personalized analysis failed without a model response")
