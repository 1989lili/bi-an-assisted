"""测试辅助：合成 K 线 + 固化真实历史数据（历史数据回放）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent / "data"


def load_real_klines(name: str) -> pd.DataFrame:
    """读取固化的真实 BTC 历史数据（历史数据回放）。

    name: 4h / 1h / 15m_breakout / 5m_flip，四周期对齐同一时刻。
    """
    return pd.read_csv(_DATA_DIR / f"real_{name}.csv")


def make_klines(
    n: int = 200,
    base: float = 100.0,
    drift: float = 0.0,
    vol: float = 0.008,
    seed: int = 7,
    tf_ms: int = 900_000,
    last_candle: str = "normal",
    last_vol_mult: float = 1.0,
) -> pd.DataFrame:
    """合成 K 线。last_candle: normal/strong_bull/strong_bear。

    strong_bull = 长实体阳线（body>shadow），用于通过 K 线形态关卡。
    """
    rng = np.random.default_rng(seed)
    closes = base * np.cumprod(1 + drift + rng.normal(0, vol, n))
    opens = np.concatenate([[closes[0]], closes[:-1]]) * (1 + rng.normal(0, vol * 0.3, n))
    highs = np.maximum(opens, closes) * (1 + rng.uniform(0.001, 0.004, n))
    lows = np.minimum(opens, closes) * (1 - rng.uniform(0.001, 0.004, n))
    volumes = rng.uniform(100, 400, n)
    ts = np.arange(n) * tf_ms

    if last_candle == "strong_bull":
        c = closes[-1]
        closes[-1] = c
        opens[-1] = c * 0.99
        highs[-1] = c * 1.001
        lows[-1] = c * 0.989
        volumes[-1] *= last_vol_mult
    elif last_candle == "strong_bear":
        c = closes[-1]
        opens[-1] = c * 1.01
        highs[-1] = c * 1.011
        lows[-1] = c * 0.999
        volumes[-1] *= last_vol_mult

    return pd.DataFrame(
        {"ts": ts, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}
    )


def make_bull_klines(tf_ms: int = 900_000, seed: int = 7) -> pd.DataFrame:
    """持续上涨的 K 线（用于构造做多场景）。"""
    return make_klines(drift=0.002, vol=0.006, seed=seed, tf_ms=tf_ms)


def make_bear_klines(tf_ms: int = 900_000, seed: int = 9) -> pd.DataFrame:
    """持续下跌的 K 线（用于构造做空场景）。"""
    return make_klines(drift=-0.002, vol=0.006, seed=seed, tf_ms=tf_ms)
