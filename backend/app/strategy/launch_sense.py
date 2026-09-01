"""标的启动感知策略（用户定制，三层漏斗）。

  第一层 日线定方向：
    - 价格锚：当前价 < MA180（日线）
    - 空间锚：(当前价 − 180天最低) / (180天最高 − 180天最低) < 50%
    两条件同时满足 → 放行（底部区域，只做多）

  第二层 1h 确认：
    BIAS = (最近1小时收盘价 − MA24) / MA24 × 100%
    LS_BIAS_MIN ≤ BIAS ≤ LS_BIAS_MAX（默认 3% ~ 8%）→ 放行

  第三层 5m 执行扳机（①量能 ②波动率 ③均线，三条件同时满足）：
    ① 量能爆发（Taker Buy Volume）：
       最近3根已收盘 5m 主动买量维持高位——
       Min(Vol_t,Vol_{t-1},Vol_{t-2}) > MA20_Vol × LS_VOL_MIN_RATIO(1.5)
       且 Min/Max ≥ LS_VOL_KEEP_RATIO(0.85)
    ② 波动率扩张（BOLL 收口 + ATR 跳升）：
       BOLL(20,2) 带宽(上轨−下轨)/中轨 < 过去60根平均带宽 × LS_BOLL_BAND_RATIO(60%)
       且 (ATR_NOW − ATR_BASE)/ATR_BASE > LS_ATR_SURGE_PCT(50%)
       且 最新价 > 当前K线 BOLL 上轨
       ATR_BASE = 过去12根已收盘 ATR(14) 均值；ATR_NOW = 含当前实时价的 ATR(14)
    ③ 均线抬头（严格模式：A/B/C/D 同根收盘验证）：
       A 位置蓄力：收盘 < MA99 × LS_MA99_CAP(1.08)
       B 站上均线：收盘 > MA7 且 > MA25
       C 金叉：MA7[前] ≤ MA25[前] 且 MA7[当前] > MA25[当前]
       D 斜率抬头：(MA7[当前] − MA7[前]) / MA7[前] > LS_MA7_SLOPE_PCT(1.0%)
       激进模式（LS_AGGRESSIVE）：C 可提前一根（上一根金叉，当前 A/B/D + 量能）
    均线用**已收盘**序列（t 为最后一根已收盘 5m K 线，避开未来函数）。

  三层全部放行 → 做多"启动"信号（不打分，前端橙色卡展示各层判定值）。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .. import config

# ==================== 指标工具 ====================

def _ma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def _bollinger(close: pd.Series, period: int = 20, mult: float = 2.0):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + mult * std
    lower = mid - mult * std
    return upper, mid, lower


def _atr14(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(14).mean()


def _last_closed_idx(df: pd.DataFrame, bar_minutes: int = 5) -> int:
    """最后一根**已收盘** K 线的索引（最后一根未收盘则取倒数第二根，避开未来函数）。"""
    import time as _t

    n = len(df)
    if n == 0:
        return -1
    last_ts = int(df["ts"].iloc[-1])
    closed = (int(_t.time() * 1000) - last_ts) >= bar_minutes * 60_000
    return n - 1 if closed else n - 2


# ==================== 三层 ====================

def layer1_daily(daily: pd.DataFrame) -> dict:
    """第一层：日线定方向（价格锚 + 空间锚）。daily 需 ≥181 根日线。"""
    if daily is None or len(daily) < 181:
        return {"pass": False, "value": None, "note": f"日线不足（{0 if daily is None else len(daily)}/181）"}
    close = daily["close"]
    price = float(close.iloc[-1])
    ma180 = float(_ma(close, 180).iloc[-1])
    hi180 = float(daily["high"].iloc[-180:].max())
    lo180 = float(daily["low"].iloc[-180:].min())
    anchor_price = price < ma180                                    # 价格锚
    anchor_pos = (price - lo180) / (hi180 - lo180) < 0.5 if hi180 > lo180 else False  # 空间锚
    ok = anchor_price and anchor_pos
    return {
        "pass": ok,
        "value": {"price": round(price, 6), "ma180": round(ma180, 6),
                  "hi180": round(hi180, 6), "lo180": round(lo180, 6),
                  "pos": round((price - lo180) / (hi180 - lo180), 3) if hi180 > lo180 else None},
        "note": f"价<MA180({'✓' if anchor_price else '✗'}) 空间{(price-lo180)/(hi180-lo180)*100:.1f}%<50%({'✓' if anchor_pos else '✗'})",
    }


def layer2_bias(h1: pd.DataFrame) -> dict:
    """第二层：1h BIAS 乖离率 ∈ [LS_BIAS_MIN, LS_BIAS_MAX]。"""
    if h1 is None or len(h1) < 25:
        return {"pass": False, "value": None, "note": "1h 数据不足（<25）"}
    close = h1["close"]
    price = float(close.iloc[-1])
    ma24 = float(_ma(close, 24).iloc[-1])
    if ma24 <= 0:
        return {"pass": False, "value": None, "note": "MA24=0"}
    bias = (price - ma24) / ma24 * 100.0
    ok = config.LS_BIAS_MIN <= bias <= config.LS_BIAS_MAX
    return {
        "pass": ok,
        "value": {"price": round(price, 6), "ma24": round(ma24, 6), "bias": round(bias, 2)},
        "note": f"BIAS={bias:.2f}%（区间 {config.LS_BIAS_MIN}~{config.LS_BIAS_MAX}%）",
    }


def _trigger_volume(taker: pd.DataFrame, t: int) -> dict:
    """第三层①：Taker Buy Volume 3 根维持高位。"""
    if taker is None or len(taker) < 21 or t < 2:
        return {"pass": False, "value": None, "note": "量能数据不足"}
    vol = taker["taker_buy"].iloc[: t + 1]
    v_t, v_1, v_2 = float(vol.iloc[-1]), float(vol.iloc[-2]), float(vol.iloc[-3])
    max_v = max(v_t, v_1, v_2)
    min_v = min(v_t, v_1, v_2)
    ma20 = float(vol.rolling(20).mean().iloc[-1])
    c1 = min_v > ma20 * config.LS_VOL_MIN_RATIO      # 绝对高位
    c2 = max_v > 0 and (min_v / max_v) >= config.LS_VOL_KEEP_RATIO  # 维持（缩量≤15%）
    ok = c1 and c2
    return {
        "pass": ok,
        "value": {"vol_t": round(v_t, 2), "vol_max": round(max_v, 2), "vol_min": round(min_v, 2),
                  "ma20": round(ma20, 2), "ratio": round(min_v / max_v, 3) if max_v > 0 else None},
        "note": f"3根量 {v_2:.1f}/{v_1:.1f}/{v_t:.1f} min>MA20×{config.LS_VOL_MIN_RATIO}({'✓' if c1 else '✗'}) "
                f"min/max={min_v/max_v:.2f}≥{config.LS_VOL_KEEP_RATIO}({'✓' if c2 else '✗'})" if max_v > 0 else "量=0",
    }


def _trigger_volatility(df5: pd.DataFrame, t: int) -> dict:
    """第三层②：波动率扩张（BOLL 收口 + ATR 跳升 + 价破上轨）。

    - 收口：已收盘序列（≤t）的当前带宽 < 过去60根带宽均值 × LS_BOLL_BAND_RATIO
    - ATR 跳升：ATR_NOW（含当前实时价序列）> ATR_BASE（过去12根已收盘均值）× (1+LS_ATR_SURGE_PCT)
    - 最新价 > 当前K线（含实时价）BOLL 上轨
    """
    if df5 is None or len(df5) < 61 or t < 60:
        return {"pass": False, "value": None, "note": "5m 数据不足"}
    closed = df5.iloc[: t + 1]
    # BOLL 带宽（已收盘序列）：当前带宽 vs 过去60根均值
    up_c, mid_c, low_c = _bollinger(closed["close"])
    band = (up_c - low_c) / mid_c
    band_now = float(band.iloc[-1])
    band_avg = float(band.iloc[-60:].mean())
    squeeze = band_now < band_avg * config.LS_BOLL_BAND_RATIO
    # ATR：BASE=过去12根已收盘 ATR14 均值；NOW=含当前实时价序列的 ATR14
    atr_closed = _atr14(closed)
    atr_base = float(atr_closed.iloc[-12:].mean())
    atr_now_series = _atr14(df5)                      # 含当前未收盘 K 线（实时价）
    atr_now = float(atr_now_series.iloc[-1])
    surge = atr_base > 0 and (atr_now - atr_base) / atr_base > config.LS_ATR_SURGE_PCT
    # 最新价 > 当前K线 BOLL 上轨（含实时价序列）
    up_full, _, _ = _bollinger(df5["close"])
    upper_now = float(up_full.iloc[-1])
    latest = float(df5["close"].iloc[-1])
    above_upper = latest > upper_now
    ok = squeeze and surge and above_upper
    return {
        "pass": ok,
        "value": {"band_now": round(band_now, 5), "band_avg": round(band_avg, 5),
                  "atr_base": round(atr_base, 6), "atr_now": round(atr_now, 6),
                  "upper": round(upper_now, 6), "latest": round(latest, 6)},
        "note": f"带宽{band_now:.4f}<均值×{config.LS_BOLL_BAND_RATIO}({'✓' if squeeze else '✗'}) "
                f"ATR跳升{(atr_now-atr_base)/atr_base*100:.0f}%>{(config.LS_ATR_SURGE_PCT)*100:.0f}%({'✓' if surge else '✗'}) "
                f"价>上轨({'✓' if above_upper else '✗'})",
    }


def _trigger_ma(df5: pd.DataFrame, t: int) -> dict:
    """第三层③：均线抬头（A/B/C/D）。严格模式同根验证；激进模式 C 可提前一根。"""
    if df5 is None or len(df5) < 100 or t < 3:
        return {"pass": False, "value": None, "note": "5m 数据不足（MA99 需 ≥100）"}
    closed = df5.iloc[: t + 1]
    close = closed["close"]
    price = float(close.iloc[-1])
    ma7 = _ma(close, 7)
    ma25 = _ma(close, 25)
    ma99 = _ma(close, 99)
    ma7_now, ma7_prev = float(ma7.iloc[-1]), float(ma7.iloc[-2])
    ma25_now, ma25_prev = float(ma25.iloc[-1]), float(ma25.iloc[-2])
    ma99_now = float(ma99.iloc[-1])
    # A 位置蓄力 / B 站上均线 / C 金叉（当前 或 前一根） / D 斜率
    a = price < ma99_now * config.LS_MA99_CAP
    b = price > ma7_now and price > ma25_now
    c_now = ma7_prev <= ma25_prev and ma7_now > ma25_now
    c_prev = False
    if t >= 3:
        ma7_p2, ma25_p2 = float(ma7.iloc[-3]), float(ma25.iloc[-3])
        c_prev = ma7_p2 <= ma25_p2 and ma7_prev > ma25_prev
    c = c_now or (config.LS_AGGRESSIVE and c_prev)   # 激进模式：金叉可提前一根
    d = ma7_prev > 0 and (ma7_now - ma7_prev) / ma7_prev * 100.0 > config.LS_MA7_SLOPE_PCT
    ok = a and b and c and d
    mode = "严格" if not config.LS_AGGRESSIVE else f"激进(C提前={'✓' if c_prev else '✗'})"
    return {
        "pass": ok,
        "value": {"price": round(price, 6), "ma7": round(ma7_now, 6), "ma25": round(ma25_now, 6),
                  "ma99": round(ma99_now, 6), "slope": round((ma7_now - ma7_prev) / ma7_prev * 100, 3) if ma7_prev > 0 else None},
        "note": f"{mode} A低位{'✓' if a else '✗'} B站上{'✓' if b else '✗'} "
                f"C金叉{'✓' if c else '✗'} D斜率{(ma7_now-ma7_prev)/ma7_prev*100:.1f}%>{config.LS_MA7_SLOPE_PCT}%({'✓' if d else '✗'})",
    }


# ==================== 组合评估 ====================

def prefilter(daily: pd.DataFrame, h1: pd.DataFrame) -> Optional[dict]:
    """预筛（第一层 + 第二层）：日线底部 + 1h BIAS。

    通过 → 返回 {"direction": "long", "layers": {layer1, layer2}}（进入 5m 监听小池）；
    不通过 → None。
    """
    layers = {
        "layer1": layer1_daily(daily),
        "layer2": layer2_bias(h1),
    }
    if not (layers["layer1"]["pass"] and layers["layer2"]["pass"]):
        return None
    return {"direction": "long", "layers": layers}


def check_l3(df5: pd.DataFrame, taker_5m: pd.DataFrame | None) -> Optional[dict]:
    """第三层执行扳机（5m K 线收盘时评估）：①量能 ②波动率 ③均线，三条件同时满足。

    通过 → {"layers": {trigger_volume, trigger_volatility, trigger_ma}}；不通过 → None。
    """
    if df5 is None or len(df5) < 100:
        return None
    t = _last_closed_idx(df5)
    if t < 2:
        return None
    layers = {
        "trigger_volume": _trigger_volume(taker_5m, t),
        "trigger_volatility": _trigger_volatility(df5, t),
        "trigger_ma": _trigger_ma(df5, t),
    }
    if not all(v["pass"] for v in layers.values()):
        return None
    return {"layers": layers}


def evaluate(klines: dict, funding_history: list[dict] | None = None,
             taker_5m: pd.DataFrame | None = None) -> Optional[dict]:
    """完整三层漏斗评估（预筛 + L3 扳机；供测试/一次性全量评估）。

    klines 需含 '1d'/'1h'/'5m'。返回 None 或 {"direction","layers","reason"}。
    """
    daily = klines.get("1d")
    h1 = klines.get("1h")
    df5 = klines.get("5m")
    if daily is None or h1 is None or df5 is None:
        return None
    pre = prefilter(daily, h1)
    if pre is None:
        return None
    l3 = check_l3(df5, taker_5m)
    if l3 is None:
        return None
    layers = {**pre["layers"], **l3["layers"]}
    return {
        "direction": "long",  # 底部启动，只做多
        "layers": layers,
        "reason": "标的启动感知：日线底部(✓) + 1h乖离(✓) + 5m量能/波动/均线扳机(✓)",
    }
