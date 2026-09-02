"""粗筛：全市场 24h 统计（单次请求）→ 动态候选池。

候选池 = 成交额 Top N ∪ |24h涨跌幅| 异动 Top N ∪ 涨幅 Top N ∪ 自选币（常驻）
"""
import logging
from typing import Optional

from .. import config
from ..store import db

logger = logging.getLogger(__name__)


class CoarseScanner:
    """每 1 分钟运行，输出候选池（ccxt symbol 列表）。"""

    def __init__(self, fetcher) -> None:
        self.fetcher = fetcher
        self._prev_pool: set[str] = set()
        self.last_pool: list[str] = []   # 最近一次成功粗筛的候选池（status 展示用，1 分钟粒度）

    def scan(self) -> list[str]:
        tickers = self.fetcher.fetch_24h_tickers()
        if not tickers:
            logger.warning("粗筛：全市场 ticker 获取失败，沿用上一轮候选池")
            return sorted(self._prev_pool)

        rows = []
        for sym, t in tickers.items():
            if not str(sym).endswith(":USDT"):
                continue
            rows.append(
                {
                    "symbol": sym,
                    "quoteVolume": t.get("quoteVolume") or 0.0,
                    "change": abs(t.get("percentage") or 0.0),
                    "gain": t.get("percentage") if t.get("percentage") is not None else float("-inf"),
                }
            )

        # 僵尸币过滤：24h 成交额 < 阈值的不入池（涨幅榜/异动榜可能带进 0 成交假币，精扫它们纯浪费）
        rows = [r for r in rows if r["quoteVolume"] >= config.CANDIDATE_MIN_QUOTE_VOLUME]
        top_vol = sorted(rows, key=lambda r: r["quoteVolume"], reverse=True)[: config.CANDIDATE_TOP_VOLUME]
        top_chg = sorted(rows, key=lambda r: r["change"], reverse=True)[: config.CANDIDATE_TOP_CHANGE]
        top_gain = sorted(rows, key=lambda r: r["gain"], reverse=True)[: config.CANDIDATE_TOP_GAIN]

        pool = {r["symbol"] for r in top_vol} | {r["symbol"] for r in top_chg} | {r["symbol"] for r in top_gain}
        pool |= db.get_watchlist()  # 自选币常驻候选池

        self._log_changes(pool)
        self.last_pool = sorted(pool)
        return sorted(pool)

    def _log_changes(self, pool: set[str]) -> None:
        entered = pool - self._prev_pool
        exited = self._prev_pool - pool
        for sym in sorted(entered):
            db.log_scan("enter", sym, "进入候选池")
            logger.info("候选池新增: %s", sym)
        for sym in sorted(exited):
            db.log_scan("exit", sym, "退出候选池")
            logger.info("候选池退出: %s", sym)
        self._prev_pool = pool
