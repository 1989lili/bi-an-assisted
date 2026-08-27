"""信号引擎单元测试（产品文档 v4 §4 十层关卡）。"""
from __future__ import annotations

import time
import unittest

from app import config
from app.indicators.engine import compute_indicator_snapshot
from app.signal.engine import SignalEngine, evaluate_exit
from app.signal.scorer import score_signal
from tests.helpers import load_real_klines, make_bear_klines, make_klines

_BAR_MS = 900_000


def _snap_dict(overrides: dict | None = None, tf: str = "15m") -> dict:
    """构造最小可用单周期快照（关卡测试用）。"""
    base = {
        "close": 100.0, "ema7": 101.0, "ema21": 99.0, "ema55": 95.0,
        "above_ema55": True, "ema7_above_21": True,
        "macd_dif": 0.5, "macd_dea": 0.3, "macd_hist": 0.2, "macd_hist_prev": -0.1,
        "macd_above_zero": True, "macd_golden": True,
        "rsi": 55.0, "rsi_prev": 45.0, "rsi_cross_up_50": True,
        "atr": 1.0, "bw": 0.05, "bw_median": 0.05,
        "adx": 30.0, "structure": "uptrend",
        "swing_highs": [103.0], "swing_lows": [97.0],
        "recent_high": 102.0, "recent_low": 97.5,
        "prev_high": 101.0, "prev_low": 98.5,
        "prev_open": 99.0, "prev_close": 100.5,
        "last_high": 100.6, "last_low": 99.4,
        "body": 0.6, "shadow": 0.3,
        "volume": 500.0, "vol_ma7": 400.0, "vol_ma21": 450.0, "volume_ratio": 1.6,
        "last_ts": int(time.time() * 1000) - _BAR_MS * 2,  # 已收盘
        "kdj": {"k": 60.0, "d": 55.0, "j": 70.0},
    }
    base.update(overrides or {})
    return base


def _bull_klines() -> dict:
    """真实 BTC 历史数据回放：方向门 + B级突破扳机 + 5m 柱翻红 + 形态全部满足。"""
    return {
        "4h": load_real_klines("4h"),
        "1h": load_real_klines("1h"),
        "15m": load_real_klines("15m_breakout"),
        "5m": load_real_klines("5m_flip"),
    }


class TestDirectionGate(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SignalEngine()

    def test_long_gate_open(self):
        s4h = _snap_dict(tf="4h")
        s1h = _snap_dict(tf="1h")
        self.assertEqual(self.engine._direction_gate(s4h, s1h), "long")

    def test_short_gate_open(self):
        s4h = _snap_dict({"above_ema55": False, "macd_above_zero": False, "macd_dif": -0.5,
                          "structure": "downtrend", "ema7": 99.0, "ema21": 100.0, "ema55": 102.0,
                          "ema7_above_21": False}, tf="4h")
        s1h = _snap_dict({"ema7_above_21": False}, tf="1h")
        self.assertEqual(self.engine._direction_gate(s4h, s1h), "short")

    def test_gate_closed_without_adx(self):
        s4h = _snap_dict({"adx": 15.0}, tf="4h")
        s1h = _snap_dict(tf="1h")
        self.assertIsNone(self.engine._direction_gate(s4h, s1h))

    def test_gate_closed_conflict(self):
        s4h = _snap_dict({"macd_above_zero": False, "macd_dif": -0.5}, tf="4h")
        s1h = _snap_dict(tf="1h")
        self.assertIsNone(self.engine._direction_gate(s4h, s1h))


class TestTrigger(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SignalEngine()

    def test_pullback_level_a_long(self):
        s15m = _snap_dict({
            "last_low": 99.2, "ema21": 99.0, "atr": 1.0,  # 低点贴近 EMA21（±0.5×ATR）
            "volume_ratio": 0.5,                          # 缩量
            "volume": 500.0, "vol_ma14": 1000.0,          # 缩量：500 < 1000×0.7
            "close": 100.0, "rsi": 45.0, "rsi_prev": 38.0,  # RSI 回升：38 < 40 ≤ 45
        })
        s5m = _snap_dict({"macd_hist": 0.2, "macd_hist_prev": -0.1}, tf="5m")
        self.assertEqual(self.engine._trigger(s15m, s5m, "long"), "A")

    def test_breakout_level_b_long(self):
        s15m = _snap_dict({
            "close": 103.0, "recent_high": 102.0,  # 突破前高
            "volume_ratio": 2.0,                    # 放量
        })
        s5m = _snap_dict({"macd_hist": 0.2, "macd_hist_prev": -0.1}, tf="5m")
        self.assertEqual(self.engine._trigger(s15m, s5m, "long"), "B")

    def test_no_5m_confirm(self):
        s15m = _snap_dict({})
        s5m = _snap_dict({"macd_hist": -0.2, "macd_hist_prev": -0.1}, tf="5m")  # 柱未翻红
        self.assertIsNone(self.engine._trigger(s15m, s5m, "long"))

    def test_level_c_rsi_cross(self):
        s15m = _snap_dict({
            "close": 100.0, "recent_high": 101.0, "volume_ratio": 1.0,
            "rsi_cross_up_50": True, "rsi": 51.0, "rsi_prev": 49.0,
        })
        s5m = _snap_dict({"macd_hist": 0.2, "macd_hist_prev": -0.1}, tf="5m")
        self.assertEqual(self.engine._trigger(s15m, s5m, "long"), "C")


class TestVolumeVeto(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SignalEngine()

    def test_veto_when_weak_volume_and_no_oi(self):
        s15m = _snap_dict({"volume_ratio": 1.0})
        self.assertFalse(self.engine._volume_veto(s15m, oi_change=0.0))

    def test_pass_when_oi_growing(self):
        s15m = _snap_dict({"volume_ratio": 1.0})
        self.assertTrue(self.engine._volume_veto(s15m, oi_change=0.02))

    def test_pass_when_volume_enough(self):
        s15m = _snap_dict({"volume_ratio": 1.5})
        self.assertTrue(self.engine._volume_veto(s15m, oi_change=0.0))


class TestRiskBrake(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SignalEngine()

    def test_pass_normal(self):
        s15m = _snap_dict({"close": 100.0, "atr": 1.0, "bw": 0.05, "bw_median": 0.05})
        s1h = _snap_dict({"swing_highs": [105.0]}, tf="1h")
        risk = self.engine._risk_brake(s15m, s1h, "long", {"tier": "normal", "position_factor": 1.0}, [])
        self.assertIsNotNone(risk)
        self.assertEqual(risk["coef"], 1.5)
        self.assertGreaterEqual(risk["risk_reward"], 2.0)
        self.assertEqual(risk["stop_loss"], 100.0 - 1.5)

    def test_block_danger_funding_long(self):
        s15m = _snap_dict({"close": 100.0, "atr": 1.0})
        s1h = _snap_dict({"swing_highs": [105.0]}, tf="1h")
        risk = self.engine._risk_brake(s15m, s1h, "long", {"tier": "danger", "position_factor": 1.0}, [])
        self.assertIsNone(risk)

    def test_block_near_liquidation_zone(self):
        from unittest import mock

        s15m = _snap_dict({"close": 100.0, "atr": 1.0, "bw": 0.05, "bw_median": 0.05})
        s1h = _snap_dict({"swing_highs": [105.0]}, tf="1h")
        # 真实预估强平价（逐仓）：现价 100，杠杆 3 → 强平价 67.17，距离 32.83 远超止损距离，正常放行
        risk = self.engine._risk_brake(s15m, s1h, "long", {"tier": "normal", "position_factor": 1.0}, [])
        self.assertIsNotNone(risk)
        # 极高杠杆（100x）→ 强平价 99.5，距离 0.5 < 止损距离 1.5 → 拦截（防爆仓）
        with mock.patch.object(config, "BINANCE_RISK_LEVERAGE", 100):
            risk = self.engine._risk_brake(s15m, s1h, "long", {"tier": "normal", "position_factor": 1.0}, [])
        self.assertIsNone(risk)

    def test_block_low_risk_reward(self):
        s15m = _snap_dict({"close": 100.0, "atr": 1.0, "bw": 0.05, "bw_median": 0.05})
        s1h = _snap_dict({"swing_highs": [101.0]}, tf="1h")  # 目标仅 +1.0 → 盈亏比 0.67
        risk = self.engine._risk_brake(s15m, s1h, "long", {"tier": "normal", "position_factor": 1.0}, [])
        self.assertIsNone(risk)


class TestCandleCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SignalEngine()

    def test_confirmed_strong_bull(self):
        # 最后一根已收盘的长实体阳线（body>shadow 且不破前低）
        s15m = _snap_dict({
            "body": 1.0, "shadow": 0.3,
            "close": 101.0, "prev_low": 98.5,
            "last_ts": int(time.time() * 1000) - _BAR_MS * 2,
        })
        self.assertEqual(self.engine._candle_check(s15m, {}, "long"), "confirmed")

    def test_rejected_by_long_shadow(self):
        s15m = _snap_dict({
            "body": 0.2, "shadow": 1.5,  # 影线 > 实体
            "close": 100.0, "prev_low": 98.5,
            "last_ts": int(time.time() * 1000) - _BAR_MS * 2,
        })
        self.assertIsNone(self.engine._candle_check(s15m, {}, "long"))

    def test_pending_wait_close(self):
        # 当前 K 线未收盘（5 分钟前开盘）且未触发旱地拔葱
        s15m = _snap_dict({
            "last_ts": int(time.time() * 1000) - 5 * 60 * 1000,
            "close": 100.0, "recent_high": 102.0, "atr": 1.0,
        })
        self.assertEqual(self.engine._candle_check(s15m, {}, "long"), "pending")

    def test_exception_breakout(self):
        # 旱地拔葱：未收盘但突破 1.5×ATR
        s15m = _snap_dict({
            "last_ts": int(time.time() * 1000) - 11 * 60 * 1000,
            "close": 104.0, "recent_high": 102.0, "atr": 1.0,  # 突破 2.0 > 1.5
        })
        self.assertEqual(self.engine._candle_check(s15m, {}, "long"), "exception")


class TestScorer(unittest.TestCase):
    def test_high_score_bull_setup(self):
        market_env = {"env": "bull", "breadth": 0.6}
        s4h = _snap_dict(tf="4h")
        s1h = _snap_dict(tf="1h")
        s15m = _snap_dict(tf="15m")
        score = score_signal(market_env, s4h, s1h, s15m, "A", {"tier": "normal"}, 2.5)
        self.assertGreaterEqual(score, 70)

    def test_low_score_bear_setup(self):
        market_env = {"env": "bear", "breadth": 0.3}
        s4h = _snap_dict(tf="4h")
        s1h = _snap_dict(tf="1h")
        s15m = _snap_dict(tf="15m")
        score = score_signal(market_env, s4h, s1h, s15m, "C", {"tier": "danger"}, 0.5)
        self.assertLess(score, 70)


class TestEvaluateEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SignalEngine()
        self.market_env_bull = {"env": "bull", "breadth": 0.6, "btc_bull": True}

    def test_bull_setup_produces_signal_or_clear_reason(self):
        klines = _bull_klines()
        card = self.engine.evaluate("TEST/USDT:USDT", klines, self.market_env_bull,
                                    [{"ts": i * 8 * 3600_000, "rate": 0.0001} for i in range(4)],
                                    oi_change=0.02, zones=[])
        if card is None:
            # 不强制出信号，但必须给出明确否决原因
            self.assertIn("TEST/USDT:USDT", self.engine.rejections)
            self.assertTrue(self.engine.rejections["TEST/USDT:USDT"])
        else:
            self.assertEqual(card.direction, "long")
            self.assertGreaterEqual(card.confidence, 50)
            self.assertIn("execution", card.to_dict())
            self.assertIn("stop_loss", card.execution)
            self.assertGreater(card.execution["risk_reward"], 0)
            self.assertEqual(card.execution["market_pct"], 70)

    def test_bull_setup_should_produce_signal(self):
        """强多头合成场景：完整链路应产出做多信号（历史数据回放验收）。"""
        klines = _bull_klines()
        card = self.engine.evaluate("TEST/USDT:USDT", klines, self.market_env_bull,
                                    [{"ts": i * 8 * 3600_000, "rate": 0.0001} for i in range(4)],
                                    oi_change=0.02, zones=[])
        if card is None:
            self.fail(f"合成强多头场景未产出信号，被拒原因: {self.engine.rejections.get('TEST/USDT:USDT')}")

    def test_bear_market_softens_long(self):
        """市场环境不做硬拦截（143a594：短线代币与 BTC 此消彼长），bear 仅影响打分加分。

        强多头合成场景 + bear 环境：允许出信号（direction=long），或给出明确否决原因。
        """
        klines = _bull_klines()
        card = self.engine.evaluate("TEST/USDT:USDT", klines,
                                    {"env": "bear", "breadth": 0.3, "btc_bull": False},
                                    [], oi_change=0.02, zones=[])
        if card is not None:
            self.assertEqual(card.direction, "long")
            self.assertGreaterEqual(card.confidence, 50)
        else:
            self.assertIn("TEST/USDT:USDT", self.engine.rejections)
            self.assertTrue(self.engine.rejections["TEST/USDT:USDT"])

    def test_insufficient_data(self):
        klines = {"4h": make_klines(n=30), "1h": make_klines(n=30),
                  "15m": make_klines(n=30), "5m": make_klines(n=30)}
        card = self.engine.evaluate("TEST/USDT:USDT", klines, self.market_env_bull, [], None, [])
        self.assertIsNone(card)
        self.assertIn("数据不足", self.engine.rejections["TEST/USDT:USDT"])


class TestExitAlerts(unittest.TestCase):
    def test_technical_reversal_long(self):
        snap = {
            "15m": _snap_dict({"ema7_above_21": False}),  # EMA7 下穿
            "1h": _snap_dict(tf="1h"),
        }
        position = {"direction": "long", "entry_price": 100.0, "stop_stage": 2, "stop_price": 98.0}
        alerts = evaluate_exit(snap, position)
        self.assertTrue(any(a["type"] == "technical_reversal" for a in alerts))

    def test_stop_approaching(self):
        snap = {
            "15m": _snap_dict({"ema7_above_21": True, "close": 98.3, "atr": 1.0}),
            "1h": _snap_dict(tf="1h"),
        }
        position = {"direction": "long", "entry_price": 100.0, "stop_stage": 1, "stop_price": 98.0}
        alerts = evaluate_exit(snap, position)
        self.assertTrue(any(a["type"] == "stop_approaching" for a in alerts))


if __name__ == "__main__":
    unittest.main()
