"""信号高频监控单元测试（SignalMonitor：过期/止损/实时价更新）。"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import pandas as pd

from app import config
from app.signal.engine import SignalCard
from app.signal.monitor import SignalMonitor
from app.store import db

_TMP_DIR = Path(tempfile.mkdtemp(prefix="bi_monitor_test_"))


def setUpModule() -> None:
    config.DATA_DIR = _TMP_DIR
    config.DB_PATH = _TMP_DIR / "test.db"
    db.init_db()


class FakeFetcher:
    """固定 K 线：最后收盘价可配置。"""

    def __init__(self, last_close: float = 100.0) -> None:
        self.last_close = last_close
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, use_cache=True, limit=None):
        self.calls.append((symbol, timeframe, use_cache, limit))
        rows = [[int(time.time() * 1000) - i * 300000, 100, 101, 99, self.last_close, 1000] for i in range(5)]
        return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])


def _make_signal(**kw) -> SignalCard:
    defaults = dict(
        symbol="BTC/USDT:USDT", direction="long", confidence=80,
        levels={}, trigger_level="B", funding={"tier": "normal"},
        execution={"market_price": 100.0, "stop_loss": 95.0, "target": 110.0, "risk_reward": 2.0},
        reason="测试信号",
    )
    defaults.update(kw)
    card = SignalCard(**defaults)
    # __post_init__ 会重算 expires_at，这里按测试参数覆盖
    if "expires_at" in kw:
        card.expires_at = kw["expires_at"]
    db.save_signal(card)
    return card


class TestMonitorBase(unittest.TestCase):
    def setUp(self) -> None:
        # 清空信号表，保证用例隔离
        with db._lock, db._connect() as conn:
            conn.execute("DELETE FROM signals")
        self.changed = []
        self.monitor = SignalMonitor(FakeFetcher())
        self.monitor.set_on_update(lambda items: self.changed.extend(items))


class TestMonitorExpired(TestMonitorBase):
    def test_expired_sets_status(self):
        card = _make_signal(expires_at=int(time.time() * 1000) - 1000)
        changed = self.monitor.check()
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["status"], "expired")
        # 已过期信号不再拉行情
        self.assertEqual(self.monitor.fetcher.calls, [])
        # 落库状态同步
        saved = db.get_signal(card.id)
        self.assertEqual(saved["status"], "expired")


class TestMonitorStoppedOut(TestMonitorBase):
    def test_long_hit_stop(self):
        card = _make_signal(direction="long", execution={"market_price": 100.0, "stop_loss": 95.0})
        self.monitor.fetcher.last_close = 94.5  # 收盘跌破止损
        changed = self.monitor.check()
        self.assertEqual(changed[0]["status"], "stopped_out")
        self.assertEqual(changed[0]["live_price"], 94.5)
        self.assertEqual(db.get_signal(card.id)["status"], "stopped_out")

    def test_short_hit_stop(self):
        card = _make_signal(direction="short", execution={"market_price": 100.0, "stop_loss": 105.0})
        self.monitor.fetcher.last_close = 105.5  # 收盘突破上方止损
        changed = self.monitor.check()
        self.assertEqual(changed[0]["status"], "stopped_out")

    def test_stop_not_hit_keeps_status(self):
        _make_signal(direction="long", execution={"market_price": 100.0, "stop_loss": 95.0})
        self.monitor.fetcher.last_close = 96.0
        changed = self.monitor.check()
        self.assertEqual(changed[0]["status"], "pending_confirm")  # 未失效则保持原状态


class TestMonitorLivePrice(TestMonitorBase):
    def test_first_update_sets_live(self):
        card = _make_signal()
        self.monitor.fetcher.last_close = 100.5
        changed = self.monitor.check()
        self.assertEqual(changed[0]["live_price"], 100.5)
        self.assertIsNotNone(changed[0]["live_updated_at"])
        self.assertEqual(changed[0]["status"], "pending_confirm")

    def test_no_change_no_broadcast(self):
        _make_signal()
        self.monitor.fetcher.last_close = 100.5
        self.monitor.check()
        self.monitor.fetcher.last_close = 100.5  # 价格不变
        changed = self.monitor.check()
        self.assertEqual(changed, [])

    def test_price_change_broadcast(self):
        _make_signal()
        self.monitor.fetcher.last_close = 100.5
        self.monitor.check()
        self.monitor.fetcher.last_close = 100.8  # 变化 0.3%
        changed = self.monitor.check()
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["live_price"], 100.8)

    def test_fetch_uses_no_cache(self):
        _make_signal()
        self.monitor.check()
        symbol, timeframe, use_cache, limit = self.monitor.fetcher.calls[0]
        self.assertEqual(timeframe, "5m")
        self.assertFalse(use_cache)  # 必须绕过缓存拿最新价
        self.assertEqual(limit, 2)   # 轻量化：只拉最新 2 根（权重 1）


class TestMonitorStopWhenExpired(TestMonitorBase):
    def test_expired_wins_over_stop(self):
        """已过期信号直接置 expired，不判止损（状态机顺序：先过期）。"""
        card = _make_signal(expires_at=int(time.time() * 1000) - 500)
        self.monitor.fetcher.last_close = 50.0  # 即使破止损
        changed = self.monitor.check()
        self.assertEqual(changed[0]["status"], "expired")


if __name__ == "__main__":
    unittest.main()
