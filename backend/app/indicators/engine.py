"""指标引擎：11 指标纯函数计算（M2）。

输入：多周期 K 线 DataFrame[ts, open, high, low, close, volume] + 费率历史 + OI
输出：指标快照 dict。全部为纯函数，便于单元测试与回测复用。

指标清单（产品文档 v4 §3）：
① 趋势：EMA7/21/55、MACD(12,26,9)、价格结构(HH/HL)、ADX(14)
② 动量：RSI(14)、KDJ(9,3,3)（情绪温度计，仅面板参考）
③ 波动：布林带(20,2)+带宽、ATR(14)
④ 量能：VOL 当前/MA7/MA21、量比
⑤ 币圈：资金费率档位(ROC)、OI 变化率、清算密集区（成交量分布估算）
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .. import config

# ==================== 基础指标 ====================


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (DIF, DEA, 柱状图)。柱状图按币安惯例 = 2×(DIF−DEA)。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR。"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(df: pd.DataFrame, period: int = 20, n_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """返回 (中轨, 上轨, 下轨, 带宽)。带宽 = (上轨−下轨)/中轨。"""
    mid = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std(ddof=0)
    upper = mid + n_std * sd
    lower = mid - n_std * sd
    bandwidth = (upper - lower) / mid.replace(0, np.nan)
    return mid, upper, lower, bandwidth


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ADX。"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_high, prev_low, prev_close = high.shift(1), low.shift(1), close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_s = tr.ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0.0)


def kdj(df: pd.DataFrame, n: int = 9, k_period: int = 3, d_period: int = 3) -> tuple[pd.Series, pd.Series, pd.Series]:
    """KDJ，仅作情绪温度计（面板参考，权重 0%）。"""
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    k = rsv.ewm(com=k_period - 1, adjust=False).mean().fillna(50.0)
    d = k.ewm(com=d_period - 1, adjust=False).mean().fillna(50.0)
    j = 3 * k - 2 * d
    return k, d, j


# ==================== 价格结构 ====================


def swing_points(df: pd.DataFrame, window: int = 5) -> tuple[list[int], list[int]]:
    """识别 swing 高低点（fractal：价格比前后 window 根都高/低）。

    返回 (高点索引列表, 低点索引列表)。
    """
    high, low = df["high"].values, df["low"].values
    highs, lows = [], []
    for i in range(window, len(df) - window):
        if high[i] == max(high[i - window : i + window + 1]):
            highs.append(i)
        if low[i] == min(low[i - window : i + window + 1]):
            lows.append(i)
    return highs, lows


def structure_label(lows: list[int], highs: list[int], close_series: pd.Series) -> str:
    """根据最近 swing 高低点判定结构：上升(HH+HL) / 下降(LH+LL) / 混合。

    规则：比较最近两个高点与最近两个低点的位置关系。
    """
    if len(highs) < 2 or len(lows) < 2:
        return "mixed"
    hh = close_series.iloc[highs[-1]] > close_series.iloc[highs[-2]]
    hl = close_series.iloc[lows[-1]] > close_series.iloc[lows[-2]]
    if hh and hl:
        return "uptrend"
    if not hh and not hl:
        return "downtrend"
    return "mixed"


# ==================== 量能 ====================


def volume_metrics(df: pd.DataFrame, ma_periods: tuple[int, int] = (7, 21)) -> dict:
    """VOL 均量 + 量比。量比 = 当前量 ÷ VOL_MA_WINDOW(14) 周期均量（标准口径）。"""
    vol = df["volume"]
    ma7 = vol.tail(ma_periods[0]).mean()
    ma14 = vol.tail(config.VOL_MA_WINDOW).mean()
    ma21 = vol.tail(ma_periods[1]).mean()
    return {
        "volume": float(vol.iloc[-1]),
        "vol_ma7": float(ma7),
        "vol_ma14": float(ma14),
        "vol_ma21": float(ma21),
        "volume_ratio": float(vol.iloc[-1] / ma14) if ma14 else None,  # 量比 = 当前VOL ÷ 14周期均量
    }


# ==================== 清算密集区（成交量分布估算） ====================


def estimate_liquidity_zones(df: pd.DataFrame, lookback: int = 100, bins: int = 30) -> list[tuple[float, float]]:
    """用成交量分布估算"清算密集区"（第一版近似方案）。

    把最近 lookback 根 K 线按价格分成 bins 个桶，成交量按收盘价落入桶，
    取成交量 ≥ 峰值×0.6 的桶作为密集区。返回 [(zone_low, zone_high), ...]。
    """
    df = df.tail(lookback)
    price_min, price_max = float(df["low"].min()), float(df["high"].max())
    if price_max <= price_min:
        return []
    span = price_max - price_min
    vol_by_bin = np.zeros(bins)
    for close, vol in zip(df["close"].values, df["volume"].values):
        idx = min(int((float(close) - price_min) / span * bins), bins - 1)
        vol_by_bin[idx] += float(vol)
    threshold = vol_by_bin.max() * 0.6
    edges = np.linspace(price_min, price_max, bins + 1)
    zones = [
        (float(edges[i]), float(edges[i + 1]))
        for i, v in enumerate(vol_by_bin)
        if v >= threshold
    ]
    return zones


def estimate_liq_price(price: float, direction: str, leverage: float, mmr: float = 0.005) -> float:
    """预估强平价格（逐仓，U 本位）。

    多头强平价 = P0 × (1 − 1/杠杆 + 维持保证金率)；空头对称。
    """
    if leverage <= 0:
        return price
    if direction == "long":
        return price * (1 - 1 / leverage + mmr)
    return price * (1 + 1 / leverage - mmr)


def nearest_zone_distance(price: float, zones: list[tuple[float, float]], direction: str) -> Optional[float]:
    """现价到最近清算密集区的距离（价格单位）。

    direction=long：检查价格下方的密集区（下跌触发清算）；
    direction=short：检查价格上方的密集区。
    返回 None 表示无密集区（估算不可用 → 放行）。
    """
    if not zones:
        return None
    if direction == "long":
        below = [price - z[1] for z in zones if z[1] < price]
        inside = any(z[0] <= price <= z[1] for z in zones)
        return min(below) if below else (0.0 if inside else None)
    above = [z[0] - price for z in zones if z[0] > price]
    inside = any(z[0] <= price <= z[1] for z in zones)
    return min(above) if above else (0.0 if inside else None)


# ==================== 资金费率档位（ROC 动态判断） ====================


def compute_funding_tier(history: list[dict]) -> dict:
    """费率档位判断（产品文档 §4-4️⃣）：看 24h 变化率，不看绝对值。

    history: [{ts, rate}] 按时间升序，最近 24h。
    返回 {tier: danger/stable_high/normal/unknown, rate, position_factor}
    """
    if not history:
        return {"tier": "unknown", "rate": None, "position_factor": 1.0}
    current = float(history[-1]["rate"])
    old = float(history[0]["rate"]) if len(history) > 1 else current
    rates = [float(h["rate"]) for h in history]

    # 危险档：24h 飙升 ≥3 倍 且 当前 > 0.03%
    if old > 0 and current > config.FUNDING_NORMAL_MAX:
        if current / old >= config.FUNDING_SURGE_TIMES:
            return {"tier": "danger", "rate": current, "position_factor": 1.0}

    # 稳定高位档：绝对值高(0.03%~0.1%) 且 24h 波动 <±30%
    if config.FUNDING_NORMAL_MAX < current <= config.FUNDING_STABLE_MAX and old > 0:
        spread = (max(rates) - min(rates)) / old
        if spread < config.FUNDING_STABLE_FLUCT:
            return {
                "tier": "stable_high",
                "rate": current,
                "position_factor": config.FUNDING_POSITION_FACTOR,
            }
    return {"tier": "normal", "rate": current, "position_factor": 1.0}


# ==================== 波动率自适应系数 ====================


def volatility_coef(bandwidth: float, bw_median: float) -> float:
    """布林带宽自适应风控系数（产品文档 §4-4️⃣）。"""
    if bw_median <= 0 or np.isnan(bandwidth) or np.isnan(bw_median):
        return config.ATR_COEF_NORMAL
    if bandwidth < bw_median * config.BW_NARROW_FACTOR:
        return config.ATR_COEF_NARROW
    if bandwidth > bw_median * config.BW_WIDE_FACTOR:
        return config.ATR_COEF_WIDE
    return config.ATR_COEF_NORMAL


# ==================== 单周期快照 ====================


def compute_tf_snapshot(df: pd.DataFrame) -> Optional[dict]:
    """计算单个周期的指标快照。数据不足（<60 根）返回 None。"""
    if df is None or len(df) < 60:
        return None
    close = df["close"]
    last, prev = close.iloc[-1], close.iloc[-2]

    dif, dea, hist = macd(close)
    # MACD 柱当前符号的连续根数（扳机动量确认放宽用：翻色后 N 根内仍视为有效）
    _hist_arr = hist.to_numpy()
    _streak = 0
    if len(_hist_arr):
        _sign = 1 if _hist_arr[-1] > 0 else (-1 if _hist_arr[-1] < 0 else 0)
        if _sign != 0:
            for _v in _hist_arr[::-1]:
                _s = 1 if _v > 0 else (-1 if _v < 0 else 0)
                if _s != _sign:
                    break
                _streak += 1
    rsi_s = rsi(close)
    atr_s = atr(df)
    _, _, _, bw = bollinger(df)
    adx_s = adx(df)
    k, d, j = kdj(df)
    ema7, ema21, ema55 = ema(close, 7), ema(close, 21), ema(close, 55)
    highs_idx, lows_idx = swing_points(df, config.SWING_WINDOW)

    vol = volume_metrics(df)
    return {
        "close": float(last),
        "last_ts": int(df["ts"].iloc[-1]),
        "recent_high": float(df["high"].iloc[-21:-1].max()),
        "recent_low": float(df["low"].iloc[-21:-1].min()),
        "ema7": float(ema7.iloc[-1]),
        "ema21": float(ema21.iloc[-1]),
        "ema55": float(ema55.iloc[-1]),
        "above_ema55": bool(last > ema55.iloc[-1]),
        "ema7_above_21": bool(ema7.iloc[-1] > ema21.iloc[-1]),
        "macd_dif": float(dif.iloc[-1]),
        "macd_dea": float(dea.iloc[-1]),
        "macd_hist": float(hist.iloc[-1]),
        "macd_hist_prev": float(hist.iloc[-2]),
        "macd_hist_streak": _streak,
        "macd_above_zero": bool(dif.iloc[-1] > 0),
        "macd_golden": bool(dif.iloc[-1] > dea.iloc[-1]),
        "rsi": float(rsi_s.iloc[-1]),
        "rsi_prev": float(rsi_s.iloc[-2]),
        "rsi_cross_up_50": bool(rsi_s.iloc[-2] < config.RSI_CROSS <= rsi_s.iloc[-1]),
        "atr": float(atr_s.iloc[-1]),
        "bw": float(bw.iloc[-1]),
        "bw_median": float(bw.rolling(config.BW_BASE_WINDOW).median().iloc[-1]),
        "adx": float(adx_s.iloc[-1]),
        "kdj": {"k": float(k.iloc[-1]), "d": float(d.iloc[-1]), "j": float(j.iloc[-1])},
        "structure": structure_label(lows_idx, highs_idx, close),
        "swing_highs": [float(close.iloc[i]) for i in highs_idx[-3:]],
        "swing_lows": [float(close.iloc[i]) for i in lows_idx[-3:]],
        "prev_high": float(df["high"].iloc[-2]),
        "prev_low": float(df["low"].iloc[-2]),
        "prev_open": float(df["open"].iloc[-2]),
        "prev_close": float(df["close"].iloc[-2]),
        "last_high": float(df["high"].iloc[-1]),
        "last_low": float(df["low"].iloc[-1]),
        "last_open": float(df["open"].iloc[-1]),
        "body": abs(float(df["close"].iloc[-1] - df["open"].iloc[-1])),
        "shadow": float(df["high"].iloc[-1] - df["low"].iloc[-1]) - abs(float(df["close"].iloc[-1] - df["open"].iloc[-1])),
        **vol,
    }


def compute_indicator_snapshot(klines: dict[str, pd.DataFrame]) -> dict:
    """多周期指标快照：输入 {'4h': df, '1h': df, '15m': df, '5m': df}。"""
    return {tf: compute_tf_snapshot(df) for tf, df in klines.items()}
