"""策略一：EMA 趋势跟踪（适合单边行情，N0.7，V2 收紧）。

- 周期对可配置（`config.EMA_TREND_TIMEFRAMES`，默认 4h 趋势 + 15m 入场）。
- **收紧入场（要"真实机会"才出信号）**：
  - 趋势周期（4h）：价格 > EMA200 **且 EMA200 明确向上/向下**（斜率>0，非走平）；斜率越强越好。
  - 入场周期（15m）：EMA20>EMA50 排列 + 金叉/**健康回踩**（触及 EMA20、从未破 EMA50、收盘站回 EMA20 上方）
    + RSI 落顺势力区（多 50~68 / 空 32~50，不追超买超卖）+ 放量（>20 均量×1.5）
    + **不追远离**（现价距 EMA20 ≤ 2×ATR）+ 收盘确认。
- 空头为多头镜像对称。
- **真实置信度评分**（55~95）：金叉/死叉、RSI 顺势度、放量倍数、EMA200 斜率、EMA20/50 分离度加权。
- 出场（三层，收盘价判定）：吊灯 3×ATR / EMA50 破位 / 时间止损（48 根）。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .. import config
from ..indicators.engine import atr, ema, rsi

_TREND_MIN_BARS = 220
_ENTRY_MIN_BARS = 60
_VOL_WINDOW = 20
_SLOPE_LOOKBACK = 4  # EMA200 斜率：对比 N 根前


def _cross_up(fast: pd.Series, slow: pd.Series) -> bool:
    return fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]


def _cross_down(fast: pd.Series, slow: pd.Series) -> bool:
    return fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]


def _retrace_reclaim(df: pd.DataFrame, fast: pd.Series, slow: pd.Series, direction: str) -> bool:
    """健康回踩：近 N 根内曾触及 EMA20（fast），但未跌破 EMA50（slow），且当前收盘重新站上/跌破 fast。

    多头：回踩到 EMA20 附近但始终未破 EMA50（支撑仍在），收盘站上 EMA20；
    空头：反抽到 EMA20 附近但始终未上破 EMA50，收盘重新跌破 EMA20。
    """
    lb = config.EMA_TREND_RETRACE_LOOKBACK
    seg = df.iloc[-lb - 1:-1]
    seg_fast = fast.iloc[-lb - 1:-1]
    seg_slow = slow.iloc[-lb - 1:-1]
    if direction == "long":
        touched = bool((seg["low"].values <= seg_fast.values).any())
        never_broke_slow = bool((seg["low"].values > seg_slow.values).all())
        return (touched and never_broke_slow
                and df["close"].iloc[-1] > fast.iloc[-1] and df["close"].iloc[-1] > slow.iloc[-1])
    touched = bool((seg["high"].values >= seg_fast.values).any())
    never_broke_slow = bool((seg["high"].values < seg_slow.values).all())
    return (touched and never_broke_slow
            and df["close"].iloc[-1] < fast.iloc[-1] and df["close"].iloc[-1] < slow.iloc[-1])


def _score_signal(rsi_v: float, vol_ratio: float, slope_pct: float,
                  cross: bool, sep_pct: float, direction: str) -> int:
    """真实置信度评分（55~95）。分数越高代表趋势/量能/动量越强。"""
    s = 55
    # 触发类型：金叉/死叉 强于 回踩/反抽
    s += 12 if cross else 6
    # RSI 顺势度：离开 55(多)/45(空) 越远越弱
    center = 55 if direction == "long" else 45
    s += max(0, int(8 - abs(rsi_v - center) * 0.6))
    # 放量倍数（>1.5 加分）
    if vol_ratio > 1.5:
        s += min(10, int((vol_ratio - 1.5) * 8))
    # EMA200 斜率（%）
    s += min(10, int(abs(slope_pct) * 400))
    # EMA20/50 分离度（%）
    s += min(8, int(sep_pct * 300))
    return int(min(95, max(55, s)))


def evaluate(klines: dict[str, pd.DataFrame]) -> Optional[dict]:
    """策略一入场评估（收紧）。返回 {'direction','confidence','reason','atr','vol_ratio'} 或 None。"""
    trend_key = config.EMA_TREND_TIMEFRAMES["trend"]
    entry_key = config.EMA_TREND_TIMEFRAMES["entry"]
    tdf = klines.get(trend_key)
    edf = klines.get(entry_key)
    if tdf is None or edf is None or len(tdf) < _TREND_MIN_BARS or len(edf) < _ENTRY_MIN_BARS:
        return None

    # ---- 趋势周期：EMA200 长期方向 + 明确斜率 ----
    tclose = tdf["close"]
    ema_long = ema(tclose, config.EMA_TREND_LONG)
    above = float(tclose.iloc[-1]) > float(ema_long.iloc[-1])
    slope_pct = (float(ema_long.iloc[-1]) - float(ema_long.iloc[-_SLOPE_LOOKBACK])) \
        / float(ema_long.iloc[-_SLOPE_LOOKBACK]) * 100

    # ---- 入场周期 ----
    eclose = edf["close"]
    efast = ema(eclose, config.EMA_TREND_FAST)
    emid = ema(eclose, config.EMA_TREND_MID)
    rsi_v = float(rsi(eclose, config.RSI_PERIOD).iloc[-1])
    vol = edf["volume"]
    vol_avg = float(vol.tail(_VOL_WINDOW).mean())
    vol_ratio = float(vol.iloc[-1]) / vol_avg if vol_avg > 0 else 0.0
    atr_v = float(atr(edf).iloc[-1])
    near_ema = abs(float(eclose.iloc[-1]) - float(efast.iloc[-1])) <= config.EMA_TREND_ENTRY_NEAR_ATR * atr_v
    sep_pct = abs(float(efast.iloc[-1]) - float(emid.iloc[-1])) / float(emid.iloc[-1]) * 100

    # ---- 多头（收紧：方向向上 + 趋势斜率 + 排列 + RSI 顺势 + 放量 + 不追远） ----
    if (above and slope_pct > 0 and float(efast.iloc[-1]) > float(emid.iloc[-1])
            and config.EMA_TREND_RSI_MIN < rsi_v <= config.EMA_TREND_RSI_MAX
            and vol_ratio > config.EMA_TREND_VOL_MULT and near_ema):
        cross = _cross_up(efast, emid)
        reclaim = _retrace_reclaim(edf, efast, emid, "long")
        if cross or reclaim:
            conf = _score_signal(rsi_v, vol_ratio, slope_pct, cross, sep_pct, "long")
            return {
                "direction": "long", "confidence": conf,
                "reason": f"EMA趋势做多（{'金叉' if cross else '回踩企稳'}，RSI {rsi_v:.0f}，放量{vol_ratio:.1f}×）",
                "atr": atr_v, "vol_ratio": vol_ratio, "slope_pct": round(slope_pct, 3),
            }

    # ---- 空头（对称镜像） ----
    if (not above and slope_pct < 0 and float(efast.iloc[-1]) < float(emid.iloc[-1])
            and config.EMA_TREND_RSI_MIN <= rsi_v < 50
            and vol_ratio > config.EMA_TREND_VOL_MULT and near_ema):
        cross = _cross_down(efast, emid)
        breakdown = _retrace_reclaim(edf, efast, emid, "short")
        if cross or breakdown:
            conf = _score_signal(rsi_v, vol_ratio, slope_pct, cross, sep_pct, "short")
            return {
                "direction": "short", "confidence": conf,
                "reason": f"EMA趋势做空（{'死叉' if cross else '反抽再破'}，RSI {rsi_v:.0f}，放量{vol_ratio:.1f}×）",
                "atr": atr_v, "vol_ratio": vol_ratio, "slope_pct": round(slope_pct, 3),
            }
    return None


def check_exit(sig: dict, entry_df: pd.DataFrame, elapsed_bars: Optional[int] = None) -> Optional[str]:
    """策略一出场判定（收盘价为准）。返回离场原因或 None（继续持有）。"""
    direction = sig["direction"]
    close = entry_df["close"]
    last_close = float(close.iloc[-1])
    emid = ema(close, config.EMA_TREND_MID)
    exec_ = sig.get("execution") or {}
    atr_v = exec_.get("atr") or float(atr(entry_df).iloc[-1])
    if atr_v <= 0:
        atr_v = float(atr(entry_df).iloc[-1])
    high = float(exec_.get("highest_close") or last_close)
    low = float(exec_.get("lowest_close") or last_close)

    # ① 吊灯止损
    if direction == "long" and last_close <= high - config.EMA_TREND_EXIT_ATR * atr_v:
        return f"吊灯止损（自高点回撤 {config.EMA_TREND_EXIT_ATR:.0f}×ATR）"
    if direction == "short" and last_close >= low + config.EMA_TREND_EXIT_ATR * atr_v:
        return f"吊灯止损（自低点反弹 {config.EMA_TREND_EXIT_ATR:.0f}×ATR）"

    # ② EMA50 收盘破位
    if direction == "long" and last_close < float(emid.iloc[-1]):
        return "收盘跌破 EMA50，趋势失效离场"
    if direction == "short" and last_close > float(emid.iloc[-1]):
        return "收盘站上 EMA50，趋势失效离场"

    # ③ 时间止损（未创新高/新低）
    if elapsed_bars is not None and elapsed_bars >= config.EMA_TREND_TIME_BARS:
        if direction == "long" and last_close <= high:
            return f"时间止损（{config.EMA_TREND_TIME_BARS} 根未创新高）"
        if direction == "short" and last_close >= low:
            return f"时间止损（{config.EMA_TREND_TIME_BARS} 根未创新低）"

    return None
