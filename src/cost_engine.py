from __future__ import annotations

from src.schemas import CostAnalysis


def _classify_cost_status(pnl_pct: float) -> str:
    if pnl_pct < -15:
        return "深度套牢"
    if pnl_pct < -5:
        return "中度套牢"
    if pnl_pct < 0:
        return "接近解套"
    if pnl_pct < 5:
        return "小幅浮盈"
    if pnl_pct < 15:
        return "浮盈中等"
    return "浮盈较大"


def generate_cost_analysis(
    close_price: float,
    cost_price: float,
    support_price: float,
    resistance_price: float,
    ma5: float,
    ma10: float,
    ma20: float,
    bias: str,
) -> CostAnalysis:
    if cost_price <= 0:
        return CostAnalysis(
            cost_status="未配置持仓成本",
            pnl_pct=0.0,
            recovery_pct=0.0,
            cost_position_analysis="当前未设置持仓成本，系统暂不计算盈亏、解套压力和成本位强弱关系。",
            cost_advice="补充持仓成本后，可结合支撑位、压力位和均线位置给出更贴近持仓状态的建议。",
        )

    pnl_pct = ((close_price - cost_price) / cost_price) * 100
    recovery_pct = ((cost_price - close_price) / close_price) * 100 if close_price < cost_price else 0.0
    cost_status = _classify_cost_status(pnl_pct)

    cost_near_ma20 = ma20 > 0 and abs(cost_price - ma20) / ma20 < 0.02
    cost_near_support = support_price > 0 and abs(cost_price - support_price) / support_price < 0.02
    cost_above_resistance = cost_price > resistance_price
    cost_between_short_ma = min(ma5, ma10) <= cost_price <= max(ma5, ma10)

    relation_parts = [f"当前持仓处于{cost_status}，相对成本的浮盈浮亏为{pnl_pct:+.2f}%"]
    if recovery_pct > 0:
        relation_parts.append(f"若要回到成本位，股价还需上涨约{recovery_pct:.2f}%")
    else:
        relation_parts.append("当前价格已高于成本位，成本压力暂不构成上方阻力")

    if cost_near_support:
        relation_parts.append("成本位接近当前支撑区，回踩该区域时需要观察承接是否稳固")
    elif cost_near_ma20:
        relation_parts.append("成本位靠近 MA20，中期均线对持仓情绪有较强参考价值")
    elif cost_between_short_ma:
        relation_parts.append("成本位落在 MA5 与 MA10 之间，短线波动会直接影响持仓体验")
    elif cost_above_resistance:
        relation_parts.append("成本位位于当前压力位上方，若未放量突破，修复到成本位仍需时间")
    else:
        relation_parts.append("成本位与主要技术位存在一定距离，短线更适合结合趋势延续性来判断")

    if bias == "bullish":
        advice = "走势偏强时可继续持有观察，重点看回踩支撑后的承接与压力位突破是否成立，避免情绪化追高或急于兑现。"
    elif bias == "bearish":
        advice = "走势偏弱时应优先控制被动风险，若反弹接近成本位或短线压力区，可结合量能决定是否减仓，避免把成本位当成必然会回到的价格。"
    else:
        advice = "当前更适合等待方向进一步明确，围绕支撑与压力位做观察，不宜仅凭接近成本位就做激进决策。"

    return CostAnalysis(
        cost_status=cost_status,
        pnl_pct=pnl_pct,
        recovery_pct=recovery_pct,
        cost_position_analysis="；".join(relation_parts) + "。",
        cost_advice=advice,
    )
