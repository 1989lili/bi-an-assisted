"""置信度评分（产品文档 v4 §5）：市场环境 20 + 趋势 30 + 时机 25 + 情绪风控 25。

硬性关卡（量能/形态/宏观/ADX）不进打分，直接否决。
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
) -> int:
    """返回 0-100 置信度。≥70 强信号 / 50-69 弱信号 / <50 不输出。"""
    score = 0

    # ---------- 市场环境 20 分 ----------
    env = market_env.get("env", "neutral")
    if env == "bull":
        score += 12
    elif env == "neutral":
        score += 6
    # bear 环境仅放行做空（对做多给 0 分）
    breadth = market_env.get("breadth", 0.5)
    score += 8 if breadth >= 0.5 else (4 if breadth >= 0.4 else 0)

    # ---------- 趋势方向 30 分 ----------
    structure = s4h.get("structure", "mixed")
    score += 12 if structure == "uptrend" else (6 if structure == "mixed" else 0)
    # EMA 体系：7>21>55 多头排列
    if s4h["ema7"] > s4h["ema21"] > s4h["ema55"]:
        score += 10
    elif s4h["ema7"] > s4h["ema21"]:
        score += 5
    # MACD：零轴上 + 金叉
    if s4h["macd_above_zero"] and s4h["macd_golden"]:
        score += 8
    elif s4h["macd_above_zero"]:
        score += 5

    # ---------- 时机确认 25 分 ----------
    score += {"A": 15, "B": 10, "C": 5}.get(trigger_level, 0)
    rsi_val = s15m["rsi"]
    if 40 <= rsi_val <= 70:  # 顺势区
        score += 10
    elif 30 <= rsi_val < 40 or 70 < rsi_val <= 80:
        score += 5
    # 超买超卖不给分

    # ---------- 情绪风控 25 分 ----------
    tier = funding_tier.get("tier", "unknown")
    score += 15 if tier == "normal" else (10 if tier == "stable_high" else (5 if tier == "unknown" else 0))
    if liq_dist_atr is None:
        score += 5  # 清算估算不可用，给中性分
    elif liq_dist_atr >= 2.0:
        score += 10
    elif liq_dist_atr >= 1.0:
        score += 6
    else:
        score += 0

    return min(score, 100)
