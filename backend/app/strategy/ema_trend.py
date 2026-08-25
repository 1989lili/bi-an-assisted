"""策略一：EMA 趋势跟踪（适合单边行情，N0.7）。

- 周期对可配置（`config.EMA_TREND_TIMEFRAMES`，默认 4h 趋势 + 15m 入场）：
  - 趋势周期（4h）：价格 vs EMA200 定长期方向，且 EMA200 向上或走平
  - 入场周期（15m）：EMA20>EMA50 排列 + 金叉/回踩再站上 + RSI + 放量（>20 均量×1.2）+ 收盘确认
- 空头为多头镜像对称（反抽再跌破 / 死叉 / RSI<50 / 放量下跌）
- 出场（三层，收盘价判定）：
  ① 吊灯止损：持仓期最高/最低收盘价 ∓ 3×ATR
  ② EMA50 收盘破位（趋势失效即走）
  ③ 时间止损：入场后 48 根入场周期 K 线未创新高/新低
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .. import config
from ..indicators.engine import atr, ema, rsi

# 数据最低根数（EMA200 需要充足预热；入场周期 60 根足够）
_TREND_MIN_BARS = 220
_ENTRY_MIN_BARS = 60
_VOL_WINDOW = 20


def _cross_up(fast: pd.Series, slow: pd.Series) -> bool:
    return fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]


def _cross_down(fast: pd.Series, slow: pd.Series) -> bool:
    return fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]


def _retrace_reclaim(df: pd.DataFrame, fast: pd.Series, slow: pd.Series, direction: str) -> bool:
    """近 N 根内曾触及 EMA20/50，当前收盘重新站上（多头）/ 重新跌破（空头）。"""
    lb = config.EMA_TREND_RETRACE_LOOKBACK
    seg_low = df["low"].iloc[-lb - 1:-1]
    seg_high = df["high"].iloc[-lb - 1:-1]
    if direction == "long":
        touched = bool((seg_low <= fast.iloc[-lb - 1:-1]).any() or (seg_low <= slow.iloc[-lb - 1:-1]).any())
        return touched and df["close"].iloc[-1] > fast.iloc[-1] and df["close"].iloc[-1] > slow.iloc[-1]
    touched = bool((seg_high >= fast.iloc[-lb - 1:-1]).any() or (seg_high >= slow.iloc[-lb - 1:-1]).any())
    return touched and df["close"].iloc[-1] < fast.iloc[-1] and df["close"].iloc[-1] < slow.iloc[-1]


def evaluate(klines: dict[str, pd.DataFrame]) -> Optional[dict]:
    """策略一入场评估。

    klines: 多周期 K 线 dict（须含 trend / entry 两个周期 key，长度需充足）。
    返回 {'direction', 'confidence', 'reason', 'atr'} 或 None。
    """
    trend_key = config.EMA_TREND_TIMEFRAMES["trend"]
    entry_key = config.EMA_TREND_TIMEFRAMES["entry"]
    tdf = klines.get(trend_key)
    edf = klines.get(entry_key)
    if tdf is None or edf is None or len(tdf) < _TREND_MIN_BARS or len(edf) < _ENTRY_MIN_BARS:
        return None

    # ---- 趋势周期：EMA200 长期方向 ----
    tclose = tdf["close"]
    ema_long = ema(tclose, config.EMA_TREND_LONG)
    above = float(tclose.iloc[-1]) > float(ema_long.iloc[-1])
    long_rising = float(ema_long.iloc[-1]) >= float(ema_long.iloc[-2])  # 向上或走平

    # ---- 入场周期 ----
    eclose = edf["close"]
    efast = ema(eclose, config.EMA_TREND_FAST)
    emid = ema(eclose, config.EMA_TREND_MID)
    rsi_v = float(rsi(eclose, config.RSI_PERIOD).iloc[-1])
    vol = edf["volume"]
    vol_ok = float(vol.iloc[-1]) > float(vol.tail(_VOL_WINDOW).mean()) * config.EMA_TREND_VOL_MULT
    atr_v = float(atr(edf).iloc[-1])

    # ---- 多头 ----
    if above and long_rising and float(efast.iloc[-1]) > float(emid.iloc[-1]) and rsi_v > 50 and vol_ok:
        cross = _cross_up(efast, emid)
        reclaim = _retrace_reclaim(edf, efast, emid, "long")
        if cross or reclaim:
            return {
                "direction": "long",
                "confidence": 75 if cross else 70,
                "reason": f"EMA趋势做多（{'金叉' if cross else '回踩企稳'}，RSI {rsi_v:.0f}，放量）",
                "atr": atr_v,
            }

    # ---- 空头（对称镜像） ----
    if not above and not long_rising and float(efast.iloc[-1]) < float(emid.iloc[-1]) and rsi_v < 50 and vol_ok:
        cross = _cross_down(efast, emid)
        breakdown = _retrace_reclaim(edf, efast, emid, "short")
        if cross or breakdown:
            return {
                "direction": "short",
                "confidence": 75 if cross else 70,
                "reason": f"EMA趋势做空（{'死叉' if cross else '反抽再破'}，RSI {rsi_v:.0f}，放量）",
                "atr": atr_v,
            }
    return None


def check_exit(sig: dict, entry_df: pd.DataFrame, elapsed_bars: Optional[int] = None) -> Optional[str]:
    """策略一出场判定（收盘价为准）。返回离场原因或 None（继续持有）。

    sig: 信号卡 dict，`execution` 中维护 `atr` / `highest_close` / `lowest_close`。
    elapsed_bars: 已过入场周期 K 线数（时间止损用）。
    """
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
