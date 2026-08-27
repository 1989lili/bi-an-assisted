"""API 路由单元测试（fastapi TestClient + 临时 SQLite）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.api.routes import router
from app.store import db

_TMP_DIR = Path(__file__).resolve().parent / "_test_tmp"
_TMP_DIR.mkdir(parents=True, exist_ok=True)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    class FakeDeep:
        last_pool = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        last_scan_ts = 1234567890000
        last_market_env = {"env": "bull", "breadth": 0.6, "btc_bull": True}

    class FakeSched:
        running = True

        def get_jobs(self):
            return []

    app.state.deep = FakeDeep()
    app.state.scheduler = FakeSched()

    class FakeFetcher:
        """position status 接口用的假行情。"""

        def fetch_ohlcv(self, symbol, timeframe):
            import pandas as pd
            import numpy as np

            rng = np.random.default_rng(1)
            n = 100
            closes = 100 * np.cumprod(1 + 0.001 + rng.normal(0, 0.005, n))
            return pd.DataFrame(
                {
                    "ts": np.arange(n) * 900_000,
                    "open": closes,
                    "high": closes * 1.01,
                    "low": closes * 0.99,
                    "close": closes,
                    "volume": rng.uniform(100, 500, n),
                }
            )

    app.state.fetcher = FakeFetcher()

    class FakeExecutor:
        """手动平仓路由用的假执行器（真单链路在 integration 层验证）。"""

        configured = True

        def cancel_order(self, symbol, order_id):
            return {"ok": True}

        def create_order(self, *a, **k):
            return {"ok": True, "dry_run": True, "id": "fake_order"}

    app.state.executor = FakeExecutor()
    return app


def setUpModule() -> None:
    _db = _TMP_DIR / "api.db"
    if _db.exists():
        _db.unlink()
    config.DATA_DIR = _TMP_DIR
    config.DB_PATH = _TMP_DIR / "api.db"
    db.init_db()


class TestApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(_make_app())


class TestStatus(TestApiBase):
    def test_status(self):
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["scheduler_running"])
        self.assertEqual(body["candidate_count"], 2)
        self.assertEqual(body["market_env"]["env"], "bull")


class TestWatchlist(TestApiBase):
    def test_crud(self):
        r = self.client.post("/api/watchlist", json={"symbol": "SOL/USDT:USDT"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

        r = self.client.get("/api/watchlist")
        self.assertIn("SOL/USDT:USDT", r.json()["symbols"])

        r = self.client.delete("/api/watchlist", params={"symbol": "SOL/USDT:USDT"})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/watchlist")
        self.assertNotIn("SOL/USDT:USDT", r.json()["symbols"])

    def test_invalid_symbol_rejected(self):
        r = self.client.post("/api/watchlist", json={"symbol": "SOL"})
        self.assertEqual(r.status_code, 400)


class TestSignals(TestApiBase):
    def test_list_and_get(self):
        from app.signal.engine import SignalCard

        card = SignalCard(
            symbol="BTC/USDT:USDT", direction="long", confidence=80,
            levels={}, trigger_level="B", funding={"tier": "normal"},
            execution={"stop_loss": 1.0}, reason="测试信号",
        )
        db.save_signal(card)

        r = self.client.get("/api/signals")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(r.json()), 1)
        self.assertEqual(r.json()[0]["symbol"], "BTC/USDT:USDT")

        r = self.client.get(f"/api/signals/{card.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["confidence"], 80)

        r = self.client.get("/api/signals/not-exist")
        self.assertEqual(r.status_code, 404)


class TestPositions(TestApiBase):
    def test_crud_and_status(self):
        r = self.client.post(
            "/api/positions",
            json={"symbol": "BTC/USDT:USDT", "direction": "long", "entry_price": 100.0, "qty": 0.1},
        )
        self.assertEqual(r.status_code, 200)
        pid = r.json()["id"]

        r = self.client.get("/api/positions")
        self.assertEqual(len(r.json()), 1)
        self.assertEqual(r.json()[0]["symbol"], "BTC/USDT:USDT")

        # 实时评估（假行情）
        r = self.client.get(f"/api/positions/{pid}/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("stage", body)
        self.assertIn("pnl_pct", body)

        # 平仓
        r = self.client.post(f"/api/positions/{pid}/close")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/positions", params={"status": "open"})
        self.assertEqual(len(r.json()), 0)

    def test_invalid_direction(self):
        r = self.client.post(
            "/api/positions",
            json={"symbol": "BTC/USDT:USDT", "direction": "up", "entry_price": 100.0, "qty": 0.1},
        )
        self.assertEqual(r.status_code, 422)


class TestMacroEvents(TestApiBase):
    def test_crud(self):
        r = self.client.post(
            "/api/macro-events",
            json={"title": "CPI", "event_time": "2026-09-10T20:30:00+08:00"},
        )
        self.assertEqual(r.status_code, 200)
        events = self.client.get("/api/macro-events").json()
        self.assertGreaterEqual(len(events), 1)
        eid = events[-1]["id"]
        r = self.client.delete(f"/api/macro-events/{eid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.client.get("/api/macro-events").json()), len(events) - 1)


class TestSettings(TestApiBase):
    def test_get_and_update(self):
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("COARSE_INTERVAL_SEC", r.json())

        r = self.client.put("/api/settings/ADX_TREND_TH", json={"value": 25})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(config.ADX_TREND_TH, 25)

        r = self.client.put("/api/settings/nonexistent_key", json={"value": 1})
        self.assertEqual(r.status_code, 400)


class TestScanLogs(TestApiBase):
    def test_list(self):
        db.log_scan("scan", None, "测试")
        r = self.client.get("/api/scan-logs")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(r.json()), 1)


if __name__ == "__main__":
    unittest.main()
