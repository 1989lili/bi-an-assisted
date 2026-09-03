"""启动感知全在线监听器（L1+L2+L3 全部 K 线收盘事件驱动）。

两条并行流（币安 fstream 组合流，websockets + 代理）：
- **1h 流**：订阅候选池全部币种 1h kline 收盘 → 实时评估 **L1(日线) + L2(1h BIAS)**，
  通过 → 进入 L3 观察集（l1l2_pool）；不再满足 → 移出。
- **5m 流**：订阅 L3 观察集币种 5m kline 收盘 → 实时评估 **L3（量能/波动/均线）**，全过爆信号。

- 候选池（watchlist）由 deep scan 每 5 分钟同步（粗筛结果，`update_watchlist`）。
- 观察集变化 → 5m 流自动重连（dirty 机制）。
- 日线 L1 数据在 1h 收盘评估时按需拉取（缓存 5 分钟，变化慢开销低）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable, Optional

from .. import config
from ..store import db

logger = logging.getLogger(__name__)


class LaunchSenseWatcher:
    """全在线三层监听：**全市场** 1h 流评估 L1+L2，5m 流评估 L3（不依赖候选池轮询）。"""

    # 单连接组合流最大订阅数（保守留余量，币安上限 200）
    _STREAM_CHUNK = 150
    # 全市场列表刷新周期（新上市合约低频，每小时刷新一次即可）
    _MARKET_REFRESH_SEC = 3600

    def __init__(self, fetcher, on_signal: Optional[Callable] = None) -> None:
        self.fetcher = fetcher
        self.on_signal = on_signal            # 回调(card)，main 广播 signal:new
        self.watchlist: set[str] = set()       # 全市场合约（1h 流订阅）
        self.l1l2_pool: set[str] = set()       # L1+L2 通过（5m 流订阅）
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._dirty = threading.Event()        # watchlist/观察集变化 → 重建流
        self._thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="launch-watch", daemon=True)
        self._thread.start()
        logger.info("启动感知全在线监听线程已启动")

    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()

    # ---------- 全市场列表 ----------

    def refresh_market(self, symbols=None) -> None:
        """刷新监控池：**24h 成交额排名 LS_RANK_START~LS_RANK_END** 的合约（默认第 20~220 名，共 200 个）。

        新增币触发初始 L1+L2 评估。symbols 传入时直接使用（测试/覆盖用）。
        """
        if symbols is None:
            tickers = self.fetcher.fetch_24h_tickers()
            if not tickers:
                return
            # 仅 USDT 计价合约（与套1/策略一主口径一致），按 24h 成交额降序取指定排名区间
            tickers = {k: v for k, v in tickers.items() if str(k).endswith(":USDT")}
            ranked = sorted(
                tickers.items(),
                key=lambda kv: float((kv[1] or {}).get("quoteVolume") or 0),
                reverse=True,
            )
            symbols = [sym for sym, _ in ranked[config.LS_RANK_START:config.LS_RANK_END]]
        new = set(symbols or [])
        with self._lock:
            added = new - self.watchlist
            self.watchlist = new
        if not added and not new:
            return
        self._dirty.set()
        logger.info("启动感知监控池同步: %s 个（成交额排名 %s~%s，新增 %s）",
                    len(new), config.LS_RANK_START, config.LS_RANK_END, len(added))
        if added:
            threading.Thread(target=self._initial_prefilter, args=(list(added),),
                             name="launch-initial", daemon=True).start()

    def _initial_prefilter(self, symbols: list[str]) -> None:
        """对新合约立即评估一次 L1+L2（并发，避免等 1h 收盘冷启动空窗）。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from ..strategy.launch_sense import prefilter

        def _eval(sym: str) -> Optional[str]:
            try:
                daily = self.fetcher.fetch_ohlcv(sym, "1d", limit=200)
                h1 = self.fetcher.fetch_ohlcv(sym, "1h", use_cache=False, limit=60)
                if daily is None or len(daily) < 181 or h1 is None:
                    return None
                return sym if prefilter(daily, h1) else None
            except Exception:  # noqa: BLE001
                return None

        passed = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for fut in as_completed([ex.submit(_eval, s) for s in symbols]):
                r = fut.result()
                if r:
                    passed.append(r)
        if passed:
            with self._lock:
                self.l1l2_pool.update(passed)
            self._dirty.set()
            logger.info("启动感知初始 L1+L2 通过 %s 个: %s", len(passed), passed[:10])

    # ---------- 主循环 ----------

    def _run(self) -> None:
        last_refresh = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_refresh >= self._MARKET_REFRESH_SEC or not self.watchlist:
                self.refresh_market()
                last_refresh = now
            try:
                asyncio.run(self._listen_all())
            except Exception as exc:  # noqa: BLE001 - 连接异常重试
                if not self._stop.is_set():
                    logger.warning("启动感知监听异常: %s（重连）", exc)
            time.sleep(2)  # 重连退避

    async def _listen_all(self) -> None:
        """并行监听：全市场 1h 流（分片连接）+ L3 观察集 5m 流。"""
        with self._lock:
            h1_syms = list(self.watchlist)
            l3_syms = list(self.l1l2_pool)
        tasks = []
        # 1h 流分片（每片 ≤150 streams，避开单连接上限）
        for i in range(0, len(h1_syms), self._STREAM_CHUNK):
            chunk = h1_syms[i:i + self._STREAM_CHUNK]
            tasks.append(self._listen_stream("1h", chunk, self._on_h1_close))
        # 5m 流（仅 L3 观察集）
        if l3_syms:
            tasks.append(self._listen_stream("5m", l3_syms, self._on_5m_close))
        if not tasks:
            await asyncio.sleep(3)
            return
        self._dirty.clear()
        logger.info("启动感知监听中: 1h×%s（%s 连接）+ 5m×%s",
                    len(h1_syms), (len(h1_syms) + self._STREAM_CHUNK - 1) // self._STREAM_CHUNK,
                    len(l3_syms))
        await asyncio.gather(*tasks, return_exceptions=True)
        # gather 返回（某流断开/超时/stop）→ 外层判断后重连

    async def _listen_stream(self, timeframe: str, symbols: list[str], handler) -> None:
        try:
            stream_names = "/".join(self._market_id(s).lower() + f"@{timeframe}" for s in symbols)
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动感知流构建失败(%s): %s", timeframe, exc)
            await asyncio.sleep(5)
            return
        url = f"wss://fstream.binance.com/stream?streams={stream_names}"
        try:
            async with _connect(url) as ws:
                while not self._stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        if self._dirty.is_set():
                            break  # 候选池/观察集变化 → 重建
                        continue
                    self._dispatch(msg, handler)
        except Exception as exc:  # noqa: BLE001 - 交给外层重连
            if not self._stop.is_set():
                raise

    def _dispatch(self, msg: str, handler) -> None:
        try:
            data = json.loads(msg)
            k = data.get("k") or {}
            if not k.get("x"):
                return  # 仅 K 线收盘事件
            mid = k.get("s") or ""
            symbol = self._to_symbol(mid)
            if symbol:
                handler(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动感知 WS 消息解析失败: %s", exc)

    # ---------- 1h 收盘 → L1+L2 ----------

    def _on_h1_close(self, symbol: str) -> None:
        """1h K 线收盘：评估 L1(日线) + L2(BIAS)，维护 L3 观察集。"""
        from ..strategy.launch_sense import prefilter

        try:
            daily = self.fetcher.fetch_ohlcv(symbol, "1d", limit=200)
            h1 = self.fetcher.fetch_ohlcv(symbol, "1h", use_cache=False, limit=60)
            if daily is None or len(daily) < 181 or h1 is None:
                return
            ok = prefilter(daily, h1) is not None
            with self._lock:
                if ok and symbol not in self.l1l2_pool:
                    self.l1l2_pool.add(symbol)
                    logger.info("启动感知 L1+L2 通过(1h收盘): %s → 进入5m观察", symbol)
                    changed = True
                elif not ok and symbol in self.l1l2_pool:
                    self.l1l2_pool.discard(symbol)
                    logger.info("启动感知 L1+L2 失效(1h收盘): %s → 移出5m观察", symbol)
                    changed = True
                else:
                    changed = False
            if changed:
                self._dirty.set()
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动感知 L1+L2 评估失败 %s: %s", symbol, exc)

    # ---------- 5m 收盘 → L3 ----------

    def _on_5m_close(self, symbol: str) -> None:
        """5m K 线收盘：评估 L3 三子条件，全过 → 出信号。"""
        from ..strategy import launch_sense as ls

        try:
            df5 = self.fetcher.fetch_ohlcv(symbol, "5m", use_cache=False, limit=150)
            taker = self.fetcher.fetch_ohlcv_taker(symbol, "5m", limit=120, use_cache=False)
            res = ls.check_l3(df5, taker)
            if res is None:
                return
            if not self._should_emit(symbol):
                logger.debug("启动感知去重跳过 %s", symbol)
                return
            card = self._build_card(symbol, res)
            db.save_signal(card)
            logger.info("🚀 启动感知信号(5m收盘实时): %s long | %s", symbol, res["reason"])
            if self.on_signal:
                self.on_signal(card)
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动感知 L3 评估失败 %s: %s", symbol, exc)

    # ---------- 信号构造 / 去重 ----------

    def _should_emit(self, symbol: str) -> bool:
        pat = '%"strategy": "launch_sense"%'
        if db.has_active_signal(symbol, "long", pat):
            return False
        if db.recent_closed_within(symbol, "long", pat, config.SIGNAL_COOLDOWN_MINUTES * 60_000):
            return False
        return True

    def _build_card(self, symbol: str, res: dict):
        from ..signal.engine import SignalCard

        df5 = self.fetcher.fetch_ohlcv(symbol, "5m", use_cache=True, limit=150)
        last_close = float(df5["close"].iloc[-1]) if df5 is not None and len(df5) else 0.0
        _LABEL = {
            "layer1": "第一层 日线定方向",
            "layer2": "第二层 1h乖离",
            "trigger_volume": "第三层① 量能爆发",
            "trigger_volatility": "第三层② 波动率扩张",
            "trigger_ma": "第三层③ 均线抬头",
        }
        levels_detail = {}
        for key, v in res["layers"].items():
            ok = v.get("pass")
            levels_detail[_LABEL.get(key, key)] = {
                "状态": "✓" if ok else ("✗" if ok is False else "待定"),
                "判定": v.get("note", ""),
            }
        exec_plan = {
            "market_price": round(last_close, 8),
            "market_pct": 0, "limit_pct": 0,
            "stop_loss": None, "target": None,
            "position_factor": 1.0,
        }
        card = SignalCard(
            symbol=symbol,
            direction="long",
            confidence=0,
            levels={"strategy": "launch_sense"},
            trigger_level="",
            funding={"tier": "unknown", "rate": None, "position_factor": 1.0},
            execution=exec_plan,
            reason=res.get("reason", "标的启动感知：三层全过"),
            strategy="launch_sense",
        )
        card.levels_detail = levels_detail
        card.status = "confirmed"
        card.id = f"{card.id}_ls"
        return card

    # ---------- symbol / market_id ----------

    def _market_id(self, symbol: str) -> str:
        return self.fetcher.exchange.market(symbol)["id"]

    def _to_symbol(self, market_id: str) -> Optional[str]:
        try:
            m = self.fetcher.exchange.markets_by_id.get(market_id)
            return m["symbol"] if m else None
        except Exception:  # noqa: BLE001
            return None


def _connect(url: str):
    """websockets 连接（带代理支持，websockets>=11）。"""
    import websockets

    kwargs = {"ping_interval": 20, "max_size": 2 ** 20}
    if config.PROXY:
        kwargs["proxy"] = config.PROXY
    return websockets.connect(url, **kwargs)
