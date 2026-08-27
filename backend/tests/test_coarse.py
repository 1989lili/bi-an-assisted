"""粗筛候选池与 K 线参数分级单元测试。

覆盖：
- D 僵尸币过滤：24h 成交额 < 100万 USDT 不入池（自选除外）
- B/E K 线根数与缓存 TTL 按周期分级、缓存 key 按 limit 隔离
"""
from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from app import config
from app.data.cache import TTLCache
from app.data.fetcher import BinanceFetcher, _estimate_weight
from app.scan.coarse import CoarseScanner
from app.store import db

_TMP_DIR = Path(__file__).resolve().parent / "_test_tmp"
_TMP_DIR.mkdir(parents=True, exist_ok=True)


def setUpModule() -> None:
    _db = _TMP_DIR / "coarse.db"
    if _db.exists():
        _db.unlink()
    config.DATA_DIR = _TMP_DIR
    config.DB_PATH = _TMP_DIR / "coarse.db"
    db.init_db()


class FakeFetcher:
    """粗筛只依赖 fetch_24h_tickers。"""

    def __init__(self, tickers: dict) -> None:
        self.tickers = tickers

    def fetch_24h_tickers(self) -> dict:
        return self.tickers


class TestZombieFilter(unittest.TestCase):
    """D：24h 成交额低于阈值的币不进候选池（三个榜单都可能带进假币）。"""

    def setUp(self) -> None:
        self.tickers = {
            "BIG/USDT:USDT": {"quoteVolume": 10_000_000, "percentage": 5.0},    # 正常
            "SMALL/USDT:USDT": {"quoteVolume": 500_000, "percentage": 99.0},    # 涨幅榜会带进它
            "ZERO/USDT:USDT": {"quoteVolume": 0, "percentage": 1000.0},         # 异动榜会带进它
        }

    def test_zombie_filtered_out(self) -> None:
        pool = CoarseScanner(FakeFetcher(self.tickers)).scan()
        self.assertIn("BIG/USDT:USDT", pool)
        self.assertNotIn("SMALL/USDT:USDT", pool)
        self.assertNotIn("ZERO/USDT:USDT", pool)

    def test_watchlist_kept_even_below_threshold(self) -> None:
        db.add_watch("SMALL/USDT:USDT")
        try:
            pool = CoarseScanner(FakeFetcher(self.tickers)).scan()
            self.assertIn("SMALL/USDT:USDT", pool)  # 自选无条件保留
        finally:
            db.remove_watch("SMALL/USDT:USDT")


class TestKlineParams(unittest.TestCase):
    """B/E：K 线根数与 TTL 按周期分级；权重预估分级；缓存 key 按 limit 隔离。"""

    def test_estimate_weight_tiers(self) -> None:
        fn = lambda: None  # noqa: E731
        fn.__name__ = "fetch_ohlcv"
        self.assertEqual(_estimate_weight(fn, ("X/USDT:USDT", "4h"), {"limit": 2}), 1)   # 监控 2 根
        self.assertEqual(_estimate_weight(fn, ("X/USDT:USDT", "4h"), {}), 2)             # 4h → 120 根
        self.assertEqual(_estimate_weight(fn, ("X/USDT:USDT", "15m"), {}), 2)            # 15m → 300 根
        self.assertEqual(_estimate_weight(fn, ("X/USDT:USDT", "4h"), {"limit": 800}), 5)
        tk = lambda: None  # noqa: E731
        tk.__name__ = "fetch_tickers"
        self.assertEqual(_estimate_weight(tk, (), {}), 40)

    def test_tf_grading_config(self) -> None:
        self.assertEqual(config.TF_KLINE_LIMIT["4h"], 120)
        self.assertEqual(config.TF_KLINE_LIMIT["1h"], 200)
        self.assertEqual(config.TF_CACHE_TTL["4h"], 900)
        self.assertEqual(config.TF_CACHE_TTL["5m"], 120)

    def test_cache_key_isolation_by_limit(self) -> None:
        """监控的 2 根缓存与精扫的 300 根缓存互不污染（key 含 limit 维度）。"""
        f = BinanceFetcher.__new__(BinanceFetcher)  # 绕过 __init__（不建真实连接）
        f._cache = TTLCache()
        f.exchange = types.SimpleNamespace(fetch_ohlcv=lambda *a, **k: None)
        calls: list[int] = []

        def fake_call(fn, symbol, timeframe, limit=None):
            calls.append(limit)
            return [[1, 1, 2, 3, 3, 10], [2, 3, 4, 5, 5, 20]]  # 2 根

        f._call = fake_call
        df2 = f.fetch_ohlcv("X/USDT:USDT", "5m", use_cache=False, limit=2)
        self.assertEqual(len(df2), 2)
        # 精扫同周期默认 limit：key 不同，必须重新拉 300 根（不会命中 2 根缓存）
        df300 = f.fetch_ohlcv("X/USDT:USDT", "5m")
        self.assertEqual(len(df300), 2)  # fake 只返回 2 根，但走的是独立 key
        self.assertEqual(calls, [2, 300])
        # use_cache=False 绕过缓存且不写回；use_cache=True 拉取后写入 2 根缓存
        f.fetch_ohlcv("X/USDT:USDT", "5m", use_cache=False, limit=2)
        f.fetch_ohlcv("X/USDT:USDT", "5m", limit=2)  # miss → 拉并写缓存
        self.assertEqual(calls, [2, 300, 2, 2])
        # 同 limit 再次请求命中缓存，不再发请求
        f.fetch_ohlcv("X/USDT:USDT", "5m", limit=2)
        self.assertEqual(calls, [2, 300, 2, 2])


if __name__ == "__main__":
    unittest.main()
