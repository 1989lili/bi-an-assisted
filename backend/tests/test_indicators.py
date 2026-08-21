"""指标引擎单元测试（产品文档 v4 §3 指标清单）。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from app.indicators.engine import (
    adx,
    atr,
    bollinger,
    compute_funding_tier,
    compute_indicator_snapshot,
    compute_tf_snapshot,
    ema,
    estimate_liquidity_zones,
    kdj,
    macd,
    nearest_zone_distance,
    rsi,
    structure_label,
    swing_points,
    volatility_coef,
    volume_metrics,
)
from tests.helpers import make_bull_klines, make_klines


class TestBaseIndicators(unittest.TestCase):
    def setUp(self) -> None:
        self.df = make_klines(n=200, drift=0.001, seed=1)

    def test_ema_length_and_tail(self):
        e = ema(self.df["close"], 7)
        self.assertEqual(len(e), 200)
        # 上涨序列：EMA7 应大于 EMA55（快线跟得紧）
        self.assertGreater(e.iloc[-1], ema(self.df["close"], 55).iloc[-1])

    def test_macd_shape_and_components(self):
        dif, dea, hist = macd(self.df["close"])
        self.assertEqual(len(dif), 200)
        self.assertTrue(np.isfinite(hist.iloc[-1]))
        # 上涨序列：DIF 应位于零轴上方（多数情况）
        self.assertGreater(dif.iloc[-1], 0)

    def test_rsi_bounds(self):
        r = rsi(self.df["close"])
        self.assertTrue(r.between(0, 100).all())
        self.assertGreater(r.iloc[-1], 50)  # 上涨序列 RSI 偏高

    def test_atr_positive(self):
        a = atr(self.df)
        self.assertGreater(a.iloc[-1], 0)

    def test_bollinger_band_relation(self):
        mid, upper, lower, bw = bollinger(self.df)
        # 前 19 根滚动窗口未满为 NaN，检查有效区间
        valid = mid.notna()
        self.assertTrue((upper[valid] >= mid[valid]).all())
        self.assertTrue((mid[valid] >= lower[valid]).all())
        self.assertGreater(bw.iloc[-1], 0)

    def test_adx_range(self):
        a = adx(self.df)
        self.assertTrue(a.between(0, 100).all())

    def test_kdj_range(self):
        k, d, j = kdj(self.df)
        self.assertTrue(k.between(0, 100).all())
        self.assertTrue(d.between(0, 100).all())

    def test_volume_metrics(self):
        m = volume_metrics(self.df)
        self.assertEqual(
            round(m["vol_ma7"], 6), round(float(self.df["volume"].tail(7).mean()), 6)
        )
        self.assertGreater(m["volume_ratio"], 0)

    def test_swing_and_structure(self):
        df = make_bull_klines()
        highs, lows = swing_points(df)
        self.assertGreater(len(highs), 0)
        label = structure_label(lows, highs, df["close"])
        self.assertIn(label, ("uptrend", "mixed"))  # 上涨序列不应判为下降

    def test_tf_snapshot_fields(self):
        snap = compute_tf_snapshot(make_klines(n=200, seed=3))
        self.assertIsNotNone(snap)
        for key in ("ema7", "ema21", "ema55", "macd_hist", "rsi", "atr", "bw", "adx",
                    "structure", "volume_ratio", "recent_high", "recent_low", "last_ts",
                    "prev_low", "prev_high", "body", "shadow", "kdj"):
            self.assertIn(key, snap, f"缺少字段 {key}")

    def test_snapshot_insufficient_data(self):
        self.assertIsNone(compute_tf_snapshot(make_klines(n=30)))

    def test_multi_tf_snapshot(self):
        klines = {
            "4h": make_klines(n=200, tf_ms=4 * 3600_000, seed=1),
            "1h": make_klines(n=200, tf_ms=3600_000, seed=2),
            "15m": make_klines(n=200, tf_ms=900_000, seed=3),
            "5m": make_klines(n=200, tf_ms=300_000, seed=4),
        }
        snap = compute_indicator_snapshot(klines)
        self.assertEqual(set(snap.keys()), {"4h", "1h", "15m", "5m"})
        self.assertTrue(all(s is not None for s in snap.values()))


class TestFundingTier(unittest.TestCase):
    def _hist(self, rates: list[float]) -> list[dict]:
        return [{"ts": i * 8 * 3600_000, "rate": r} for i, r in enumerate(rates)]

    def test_danger_surge(self):
        # 24h 内费率 0.01% → 0.05%（5 倍），危险档
        h = self._hist([0.0001, 0.0002, 0.0003, 0.0005])
        tier = compute_funding_tier(h)
        self.assertEqual(tier["tier"], "danger")
        self.assertEqual(tier["position_factor"], 1.0)

    def test_stable_high(self):
        # 0.05% 附近小幅波动，稳定高位档
        h = self._hist([0.00048, 0.00050, 0.00052, 0.00050])
        tier = compute_funding_tier(h)
        self.assertEqual(tier["tier"], "stable_high")
        self.assertEqual(tier["position_factor"], 0.7)

    def test_normal(self):
        h = self._hist([0.00008, 0.00009, 0.00007, 0.00008])
        tier = compute_funding_tier(h)
        self.assertEqual(tier["tier"], "normal")

    def test_empty(self):
        self.assertEqual(compute_funding_tier([])["tier"], "unknown")


class TestVolatilityCoef(unittest.TestCase):
    def test_three_regimes(self):
        median = 0.1
        # 收缩：带宽 < 0.075
        self.assertEqual(volatility_coef(0.05, median), 1.0)
        # 正常
        self.assertEqual(volatility_coef(0.10, median), 1.5)
        # 扩张：带宽 > 0.15
        self.assertEqual(volatility_coef(0.20, median), 2.0)

    def test_invalid_inputs(self):
        self.assertEqual(volatility_coef(float("nan"), 0.1), 1.5)


class TestLiquidityZones(unittest.TestCase):
    def test_zones_and_distance(self):
        df = make_klines(n=100, base=100.0, vol=0.002, seed=5)
        zones = estimate_liquidity_zones(df)
        self.assertGreater(len(zones), 0)
        for lo, hi in zones:
            self.assertLess(lo, hi)
        # 现价 105（高于所有密集区）→ 做多方向应返回有限距离
        d = nearest_zone_distance(105.0, zones, "long")
        self.assertIsNotNone(d)
        self.assertGreaterEqual(d, 0)
        # 无密集区 → None（放行）
        self.assertIsNone(nearest_zone_distance(105.0, [], "long"))


if __name__ == "__main__":
    unittest.main()
