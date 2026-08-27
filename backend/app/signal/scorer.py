"""置信度评分（用户定稿评分表 v5）：市场环境 20 + 趋势强度 30 + 时机 25 + 风控 25 + 量能加分。

通过线 `SCORE_PASS`（当前 60）；量能加分为额外加分，总分封顶 100（避免标准失真）。

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


def _compute(
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
) -> dict:
    """内部计算：返回 {total, items:[{cat,name,score,max,note}]}。"""
    items: list[dict] = []
    score = 0

    def add(cat: str, name: str, s: int, max_: int, note: str) -> None:
        nonlocal score
        score += s
        items.append({"cat": cat, "name": name, "score": s, "max": max_, "note": note})

    tier = funding_tier.get("tier", "unknown")
    tier_score = {None: 0, "normal": 5, "stable_high": 3, "surge": 1, "danger": 0, "unknown": 2}

    # ---------- 市场环境 20 ----------
    s_env = tier_score.get(tier, 2)
    add("市场环境", "资金费率正常", s_env, 5, f"tier={tier}")
    prev_close = s15m.get("prev_close")
    price_up = bool(prev_close is not None and s15m["close"] > prev_close)
    price_dn = bool(prev_close is not None and s15m["close"] < prev_close)
    oi = oi_change or 0.0
    if oi >= config.OI_GROWTH_VETO and ((direction == "long" and price_up) or (direction == "short" and price_dn)):
        add("市场环境", "OI 与价格同向", 5, 5, f"OI {oi*100:.1f}% 且价格沿持仓方向")
    elif oi >= config.OI_GROWTH_VETO:
        add("市场环境", "OI 与价格同向", 1, 5, f"OI {oi*100:.1f}% 但价格反向（分歧）")
    elif oi <= -config.OI_GROWTH_VETO:
        add("市场环境", "OI 与价格同向", 2, 5, f"OI {oi*100:.1f}%（减仓存量博弈）")
    else:
        add("市场环境", "OI 与价格同向", 2, 5, f"OI {oi*100:.1f}%（持平）")
    if next_event_mins is None or next_event_mins > 240:
        add("市场环境", "无重大事件", 5, 5, "距最近宏观事件 >4h（或无）")
    elif next_event_mins > 60:
        add("市场环境", "无重大事件", 3, 5, f"距最近事件约 {int(next_event_mins)} 分钟")
    else:
        add("市场环境", "无重大事件", 1, 5, f"距最近事件仅 {int(next_event_mins)} 分钟")
    bw_ratio = (s15m.get("bw") or 0) / (s15m.get("bw_median") or 1.0) if (s15m.get("bw_median") or 0) > 0 else 1.0
    if 0.5 <= bw_ratio <= 1.5:
        add("市场环境", "波动率适中", 5, 5, f"带宽/中位={bw_ratio:.2f}")
    elif 1.5 < bw_ratio <= 2.0:
        add("市场环境", "波动率适中", 3, 5, f"带宽/中位={bw_ratio:.2f}（偏高）")
    else:
        add("市场环境", "波动率适中", 1, 5, f"带宽/中位={bw_ratio:.2f}（过窄/过宽）")

    # ---------- 趋势强度 30 ----------
    slope = s4h.get("ema55_slope_pct", 0.0)
    if slope > 0.05:
        add("趋势强度", "4h EMA55 向上", 10, 10, f"斜率 {slope:.2f}%")
    elif slope >= -0.05:
        add("趋势强度", "4h EMA55 向上", 6, 10, f"斜率 {slope:.2f}%（走平）")
    else:
        add("趋势强度", "4h EMA55 向上", 2, 10, f"斜率 {slope:.2f}%（向下）")
    add("趋势强度", "1h EMA7>EMA21", 10 if s1h.get("ema7_above_21") else 4, 10,
        "满足" if s1h.get("ema7_above_21") else "不满足")
    adx = s4h.get("adx") or 0.0
    if adx >= 25:
        add("趋势强度", "ADX 趋势强度", 10, 10, f"ADX={adx:.1f}")
    elif adx >= 20:
        add("趋势强度", "ADX 趋势强度", 6, 10, f"ADX={adx:.1f}（20~25）")
    else:
        add("趋势强度", "ADX 趋势强度", 2, 10, f"ADX={adx:.1f}（<20）")

    # ---------- 时机 25 ----------
    lv = {"A": 15, "B": 10, "C": 5}.get(trigger_level, 5)
    lv_note = {"A": "A级回踩（缩量企稳+RSI回升）", "B": "B级突破（放量破前高）",
               "C": "C级 RSI 穿越 50（无量能）"}.get(trigger_level, trigger_level)
    add("时机", "扳机级别", lv, 15, lv_note)
    streak = int(macd_streak or 0)
    if streak == 1:
        add("时机", "5m MACD 同向刚启动", 5, 5, "柱刚翻色（streak=1）")
    elif 2 <= streak <= 3:
        add("时机", "5m MACD 同向刚启动", 3, 5, f"柱延续 {streak} 根")
    else:
        add("时机", "5m MACD 同向刚启动", 1, 5, f"柱延续 {streak} 根（动量已走一段）")

    # ---------- 风控 25 ----------
    rr = risk_reward
    if rr is None:
        add("风控", "盈亏比 ≥2", 6, 10, "盈亏比不可用（中性）")
    elif rr >= 2.5:
        add("风控", "盈亏比 ≥2", 10, 10, f"RR={rr:.2f}")
    elif rr >= config.MIN_RISK_REWARD:
        add("风控", "盈亏比 ≥2", 8, 10, f"RR={rr:.2f}")
    else:
        add("风控", "盈亏比 ≥2", 4, 10, f"RR={rr:.2f}（不足）")
    if liq_dist_atr is None:
        add("风控", "止损距离合理", 5, 10, "强平价距离不可用（中性）")
    elif liq_dist_atr >= 2.0:
        add("风控", "止损距离合理", 10, 10, f"强平价距离 {liq_dist_atr:.1f}×ATR（充裕）")
    elif liq_dist_atr >= 1.0:
        add("风控", "止损距离合理", 6, 10, f"强平价距离 {liq_dist_atr:.1f}×ATR（可接受）")
    else:
        add("风控", "止损距离合理", 2, 10, f"强平价距离 {liq_dist_atr:.1f}×ATR（偏近）")
    s_fee = tier_score.get(tier, 2)
    add("风控", "费率安全", s_fee, 5, f"tier={tier}")

    # ---------- 量能加分（额外，封顶 100） ----------
    if volume_ratio is not None:
        oi_ok = oi >= config.OI_GROWTH_VETO
        hot = volume_ratio >= config.VOL_RATIO_HOT
        low = volume_ratio <= config.VOL_RATIO_LOW
        structure = s4h.get("structure", "mixed")
        one_sided = structure in ("uptrend", "downtrend")
        if hot and oi_ok:
            add("量能加分", "放量+OI 增", config.VOL_SCORE_STRONG, 8,
                f"量比 {volume_ratio:.2f} + OI {oi*100:.1f}%（真突破）")
        elif hot or oi_ok:
            add("量能加分", "放量或 OI 单增", config.VOL_SCORE_MILD, 8,
                f"量比 {volume_ratio:.2f} / OI {oi*100:.1f}%")
        elif low and one_sided:
            add("量能加分", "单边趋势缩量回踩", config.VOL_SCORE_MILD, 8,
                f"量比 {volume_ratio:.2f}（{structure} 蓄势）")
        elif low:
            add("量能加分", "缩量", 2, 8, f"量比 {volume_ratio:.2f}（非单边）")

    return {"total": min(score, 100), "items": items}


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
    """返回 0-100 置信度。≥SCORE_PASS 出信号；加分不使总分超过 100。"""
    return _compute(
        market_env, s4h, s1h, s15m, trigger_level, funding_tier, liq_dist_atr,
        direction=direction, risk_reward=risk_reward, macd_streak=macd_streak,
        volume_ratio=volume_ratio, oi_change=oi_change, next_event_mins=next_event_mins,
    )["total"]


def score_breakdown(
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
) -> dict:
    """评分明细：{total, pass_line, items:[{cat,name,score,max,note}]}（前端点击分数弹窗用）。"""
    res = _compute(
        market_env, s4h, s1h, s15m, trigger_level, funding_tier, liq_dist_atr,
        direction=direction, risk_reward=risk_reward, macd_streak=macd_streak,
        volume_ratio=volume_ratio, oi_change=oi_change, next_event_mins=next_event_mins,
    )
    res["pass_line"] = config.SCORE_PASS
    return res
