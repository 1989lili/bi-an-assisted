"""置信度评分（用户定稿评分表 v5）：市场环境 20 + 趋势强度 30 + 时机 25 + 风控 25 + 量能加分。

通过线 `SCORE_PASS=70`；量能加分为额外加分，总分封顶 100（避免标准失真）。

评分表（每个子项给出明确得分条件）：
  市场环境 20  资金费率正常 +5 ｜ OI 与价格同向 +5 ｜ 无重大事件 +5 ｜ 波动率适中 +5
  趋势强度 30  4h EMA55 向上 +10 ｜ 1h EMA7>EMA21 +10 ｜ ADX≥25 +10（按程度给分）
  时机 25      A级回踩 +15 ｜ B级突破 +10 ｜ 5m MACD 同向刚启动 +5
  风控 25      盈亏比≥2 +10 ｜ 止损距离合理 +10 ｜ 费率安全 +5
  量能加分     放量+OI增 +8 ｜ 单边趋势缩量回踩 +4（或放量/OI 单增 +4）

硬性关卡（量能/形态/宏观/ADX）不进打分，直接否决（signal/engine.py）。
"""
from __future__ import annotations

from .. import config


def score_signal(
    market_env: dict,
    s4h: dict,
    s1h: dict,
    s15m: dict,
    trigger_level: str,
    funding_tier: dict,
    liq_dist_atr: float | None,
    direction: str = "long",
    risk_reward: float | None = None,
    macd_streak: int | None = None,
    volume_ratio: float | None = None,
    oi_change: float | None = None,
    next_event_mins: float | None = None,
) -> int:
    """返回 0-100 置信度。≥SCORE_PASS(70) 出信号；加分不使总分超过 100。"""
    score = 0

    # ---------- 市场环境 20 ----------
    tier = funding_tier.get("tier", "unknown")
    # 资金费率正常 +5（normal 满；stable_high/surge 递减；danger 0）
    score += {None: 0, "normal": 5, "stable_high": 3, "surge": 1, "danger": 0, "unknown": 2}.get(tier, 2)
    # OI 与价格同向 +5：增仓（≥+1%）且价格沿持仓方向移动
    prev_close = s15m.get("prev_close")
    price_up = bool(prev_close is not None and s15m["close"] > prev_close)
    price_dn = bool(prev_close is not None and s15m["close"] < prev_close)
    oi = oi_change or 0.0
    if oi >= config.OI_GROWTH_VETO and ((direction == "long" and price_up) or (direction == "short" and price_dn)):
        score += 5          # 增仓 + 顺向价格
    elif oi >= config.OI_GROWTH_VETO:
        score += 1          # 增仓但价格反向（多空分歧）
    elif oi <= -config.OI_GROWTH_VETO:
        score += 2          # 减仓（存量博弈）
    else:
        score += 2          # OI 持平
    # 无重大事件 +5：最近宏观事件 >4h（或无）视为安全窗口；<1h 罕见（引擎已静默拦截）
    if next_event_mins is None or next_event_mins > 240:
        score += 5
    elif next_event_mins > 60:
        score += 3
    else:
        score += 1
    # 波动率适中 +5：15m 带宽相对中位数适中（过窄蓄势/过宽高危均降分）
    bw_ratio = (s15m.get("bw") or 0) / (s15m.get("bw_median") or 1.0) if (s15m.get("bw_median") or 0) > 0 else 1.0
    if 0.5 <= bw_ratio <= 1.5:
        score += 5
    elif 1.5 < bw_ratio <= 2.0:
        score += 3
    else:
        score += 1

    # ---------- 趋势强度 30 ----------
    # 4h EMA55 向上 +10（斜率 >0.05% 满；平 +6；向下 +2）
    slope = s4h.get("ema55_slope_pct", 0.0)
    score += 10 if slope > 0.05 else (6 if slope >= -0.05 else 2)
    # 1h EMA7>EMA21 +10
    score += 10 if s1h.get("ema7_above_21") else 4
    # ADX 按程度：≥25 +10；20~25 +6；<20 +2（方向门已开，理论 ≥20）
    adx = s4h.get("adx") or 0.0
    score += 10 if adx >= 25 else (6 if adx >= 20 else 2)

    # ---------- 时机 25 ----------
    score += {"A": 15, "B": 10, "C": 5}.get(trigger_level, 5)
    # 5m MACD 同向刚启动 +5（streak=1 刚翻色；2~3 延续；>3 动量已走一段）
    streak = int(macd_streak or 0)
    score += 5 if streak == 1 else (3 if 2 <= streak <= 3 else 1)

    # ---------- 风控 25 ----------
    rr = risk_reward
    if rr is None:
        score += 6  # 盈亏比不可用时中性分
    elif rr >= 2.5:
        score += 10
    elif rr >= config.MIN_RISK_REWARD:
        score += 8
    else:
        score += 4
    # 止损距离合理 +10（强平价距离：≥2×ATR 充裕 / ≥1 可接受 / <1 危险）
    if liq_dist_atr is None:
        score += 5
    elif liq_dist_atr >= 2.0:
        score += 10
    elif liq_dist_atr >= 1.0:
        score += 6
    else:
        score += 2
    # 费率安全 +5（与市场环境"费率正常"互补：此处看绝对安全档）
    score += {None: 0, "normal": 5, "stable_high": 3, "surge": 1, "danger": 0, "unknown": 2}.get(tier, 2)

    # ---------- 量能加分（额外，封顶 100） ----------
    if volume_ratio is not None:
        oi_ok = oi >= config.OI_GROWTH_VETO
        hot = volume_ratio >= config.VOL_RATIO_HOT
        low = volume_ratio <= config.VOL_RATIO_LOW
        structure = s4h.get("structure", "mixed")
        one_sided = structure in ("uptrend", "downtrend")
        if hot and oi_ok:
            score += config.VOL_SCORE_STRONG        # 放量 + OI 增：真突破 +8
        elif hot or oi_ok:
            score += config.VOL_SCORE_MILD          # 放量或 OI 单增 +4
        elif low and one_sided:
            score += config.VOL_SCORE_MILD          # 单边趋势中缩量回踩蓄势 +4
        elif low:
            score += 2                              # 缩量但非单边 +2

    return min(score, 100)
