"""精扫：候选池 → 多周期 K线/费率/OI → 指标引擎 → 信号引擎（M2 完整接入）。

M2 起：每 5 分钟运行，对候选池执行完整 10 层关卡流水线，产出信号卡落库。
M5 起：候选池并行扫描（SCAN_CONCURRENCY 路），请求级限速由 fetcher 权重预算保护。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from .. import config
from ..indicators.engine import estimate_liquidity_zones
from ..signal.engine import SignalEngine
from ..store import db

logger = logging.getLogger(__name__)


class DeepScanner:
    """每 5 分钟运行，对候选池做深度扫描。"""

    def __init__(self, fetcher, coarse_scanner, engine: Optional[SignalEngine] = None) -> None:
        self.fetcher = fetcher
        self.coarse = coarse_scanner
        self.engine = engine or SignalEngine()
        # 最近一轮状态（API /api/status 读取）
        self.last_pool: list[str] = []
        self.last_scan_ts: Optional[int] = None
        self.last_market_env: dict | None = None

    def scan(self) -> list[dict]:
        pool = self.coarse.scan()
        self.last_pool = pool
        self.last_scan_ts = int(time.time() * 1000)
        market_env = self._market_env()
        self.last_market_env = market_env

        # 并发扫描候选池（线程安全：db 层全局锁、engine.rejections 单赋值、结果收集在 GIL 下原子）
        signals = []
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=config.SCAN_CONCURRENCY) as ex:
            futures = {ex.submit(self._scan_symbol, sym, market_env): sym for sym in pool}
            for fut in as_completed(futures):
                signal = fut.result()
                if signal is not None:
                    signals.append(signal)
        logger.info(
            "精扫完成: 候选池 %s 个，信号 %s 条，耗时 %.1fs",
            len(pool), len(signals), time.monotonic() - t0,
        )

        # 费率快照落库（供 ROC 判断与异动榜使用）
        self._persist_funding(pool)
        return signals

    # ---------- 0️⃣ 市场环境层 ----------

    def _market_env(self) -> dict:
        """BTC 4h 方向 + 全市场涨跌家数比 → bull/bear/neutral。"""
        tickers = self.fetcher.fetch_24h_tickers()
        breadth = 0.5
        if tickers:
            up = sum(1 for t in tickers.values() if isinstance(t, dict) and (t.get("percentage") or 0) > 0)
            total = sum(1 for t in tickers.values() if isinstance(t, dict) and t.get("percentage") is not None)
            breadth = up / total if total else 0.5

        btc_bull = None
        df = self.fetcher.fetch_ohlcv("BTC/USDT:USDT", config.TIMEFRAMES["direction"])
        if df is not None and not df.empty:
            close = df["close"]
            ema55 = close.ewm(span=55, adjust=False).mean().iloc[-1]
            btc_bull = bool(close.iloc[-1] > ema55)

        if btc_bull is True and breadth >= 0.5:
            env = "bull"
        elif btc_bull is False and breadth < 0.5:
            env = "bear"
        else:
            env = "neutral"
        return {"env": env, "breadth": round(breadth, 3), "btc_bull": btc_bull}

    # ---------- 单币扫描 ----------

    def _scan_symbol(self, symbol: str, market_env: dict):
        """拉 4 周期 K线 + OI + 费率 + 清算密集区 → 信号引擎流水线。"""
        klines = {}
        for tf in config.TIMEFRAMES.values():
            df = self.fetcher.fetch_ohlcv(symbol, tf)
            if df is None or df.empty:
                return None
            klines[tf] = df

        oi_change = self._oi_change(symbol)
        funding_history = db.get_funding_history(symbol, hours=24)
        zones = estimate_liquidity_zones(klines["15m"])

        signal = self.engine.evaluate(symbol, klines, market_env, funding_history, oi_change, zones)
        if signal is not None:
            db.save_signal(signal)
            logger.info("🔥 信号: %s %s | %s 分 | %s", signal.symbol, signal.direction, signal.confidence, signal.reason)
        else:
            logger.debug("无信号 %s: %s", symbol, self.engine.rejections.get(symbol, ""))
        return signal

    def _oi_change(self, symbol: str) -> Optional[float]:
        """OI 近 30 分钟变化率（最近 6 根 5m 数据）。"""
        df = self.fetcher.fetch_oi_history(symbol, timeframe="5m", limit=6)
        if df is None or len(df) < 2:
            return None
        first = float(df["openInterest"].iloc[0])
        last = float(df["openInterest"].iloc[-1])
        if first <= 0:
            return None
        return (last - first) / first

    # ---------- 费率持久化 ----------

    def _persist_funding(self, pool: list[str]) -> None:
        rates = self.fetcher.fetch_funding_rates()
        if not rates:
            return
        flat = {}
        for sym in pool:
            r = rates.get(sym)
            if r and r.get("fundingRate") is not None:
                flat[sym] = float(r["fundingRate"])
        if flat:
            db.save_funding_rates(flat, int(time.time() * 1000))
