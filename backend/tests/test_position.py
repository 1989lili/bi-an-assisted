"""持仓管理器单元测试：止损三段式状态机（产品文档 v4 §4-8️⃣）。"""
from __future__ import annotations

import unittest

from app.position.manager import evaluate_stage, initial_stop, position_snapshot


def _pos(direction="long", entry=100.0, qty=1.0, stage=1, stop=None) -> dict:
    return {"direction": direction, "entry_price": entry, "qty": qty, "stage": stage, "stop_price": stop}


class TestInitialStop(unittest.TestCase):
    def test_long(self):
        self.assertEqual(initial_stop(100.0, "long", 1.5, 2.0), 97.0)

    def test_short(self):
        self.assertEqual(initial_stop(100.0, "short", 2.0, 2.0), 104.0)


class TestStageTransitions(unittest.TestCase):
    def test_stage1_hold(self):
        r = evaluate_stage(_pos(), current_price=101.0, atr=1.0)
        self.assertEqual(r["action"], "hold")
        self.assertEqual(r["stage"], 1)

    def test_stage1_to_2_be_profit(self):
        # 浮盈 1.6×ATR ≥ 1.5 → 保本
        r = evaluate_stage(_pos(), current_price=101.6, atr=1.0)
        self.assertEqual(r["stage"], 2)
        self.assertEqual(r["action"], "move_stop")
        self.assertEqual(r["stop_price"], 100.0)  # 止损移至入场价

    def test_stage2_to_3_ema21_trail(self):
        # 浮盈 3.2×ATR ≥ 3 → EMA21 跟踪
        r = evaluate_stage(_pos(stage=2, stop=100.0), current_price=103.2, atr=1.0, ema21_1h=102.5)
        self.assertEqual(r["stage"], 3)
        self.assertEqual(r["stop_price"], 102.5)

    def test_stage3_trail_up(self):
        # 阶段三：EMA21 上移 → 止损跟随
        r = evaluate_stage(_pos(stage=3, stop=102.0), current_price=104.0, atr=1.0, ema21_1h=103.0)
        self.assertEqual(r["action"], "move_stop")
        self.assertEqual(r["stop_price"], 103.0)

    def test_stage3_exit_break_ema21(self):
        # 阶段三：1h 收盘跌破 EMA21 → 离场
        r = evaluate_stage(_pos(stage=3, stop=102.0), current_price=101.5, atr=1.0, ema21_1h=102.0)
        self.assertEqual(r["action"], "exit")

    def test_stop_hit_exit(self):
        r = evaluate_stage(_pos(stage=1, stop=97.0), current_price=96.8, atr=1.0)
        self.assertEqual(r["action"], "exit")
        self.assertIn("触及止损", r["reason"])

    def test_short_side_be_profit(self):
        r = evaluate_stage(_pos(direction="short", entry=100.0), current_price=98.4, atr=1.0)
        self.assertEqual(r["stage"], 2)
        self.assertEqual(r["stop_price"], 100.0)


class TestSnapshot(unittest.TestCase):
    def test_snapshot_fields(self):
        snap = position_snapshot(_pos(), current_price=101.0, atr=1.0, ema21_1h=100.5)
        self.assertIn("pnl_pct", snap)
        self.assertIn("action_reason", snap)
        self.assertEqual(snap["price"], 101.0)
        self.assertGreater(snap["pnl_pct"], 0)  # 做多上涨浮盈为正


if __name__ == "__main__":
    unittest.main()
