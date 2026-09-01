"""启动感知 5m K 线监听器（实时模式）。

订阅「L1+L2 预筛小池」币种的 5m kline 收盘流（币安 fstream 组合流），
每根 5m K 线**收盘瞬间**评估第三层三子条件（量能/波动率/均线），全过即爆信号。

- 小池由 deep scan 预筛（日线 L1 + 1h L2）维护，`update_pool()` 变化时触发重连。
- 出信号逻辑与扫描版一致（去重/冷却/橙色卡/levels_detail 各层判定值）。
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

_STREAM_SUFFIX = "@kline_5m"
_MONITORED_STATUSES = ("pending_confirm", "confirmed")


class LaunchSenseWatcher:
    """监听小池 5m K 线收盘 → 实时评估 L3 → 爆信号。"""

    def __init__(self, fetcher, on_signal: Optional[Callable] = None) -> None:
        self.fetcher = fetcher
        self.on_signal = on_signal          # 回调(card)，main 广播 signal:new
        self.pool: set[str] = set()          # 观察小池（L1+L2 预筛通过）
        self._stop = threading.Event()
        self._dirty = threading.Event()      # pool 变化 → 重连
        self._thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="launch-watch", daemon=True)
        self._thread.start()
        logger.info("启动感知 5m 监听线程已启动")

    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()

    def update_pool(self, symbols) -> None:
        """更新观察小池（deep scan 预筛后调用）；池变化才触发重连。"""
        new_pool = set(symbols or [])
        if new_pool == self.pool:
            return
        removed = self.pool - new_pool
        added = new_pool - self.pool
        self.pool = new_pool
        self._dirty.set()
        logger.info("启动感知观察小池更新: +%s -%s 共 %s 个",
                    sorted(added), sorted(removed), len(new_pool))

    # ---------- 主循环 ----------

    def _run(self) -> None:
        while not self._stop.is_set():
            pool = list(self.pool)
            if not pool:
                time.sleep(3)
                continue
            try:
                asyncio.run(self._listen(pool))
            except Exception as exc:  # noqa: BLE001 - 连接异常重试
                if not self._stop.is_set():
                    logger.warning("启动感知监听连接异常: %s（重连）", exc)
            time.sleep(2)  # 重连退避

    async def _listen(self, pool: list[str]) -> None:
        """连接组合流并收消息；pool 变化（dirty）或 stop 时退出。"""
        try:
            stream_names = "/".join(self._market_id(s).lower() + _STREAM_SUFFIX for s in pool)
        except Exception as exc:  # noqa: BLE001 - 个别币种 market 信息缺失时跳过整池
            logger.warning("启动感知 stream 构建失败: %s", exc)
            return
        url = f"wss://fstream.binance.com/stream?streams={stream_names}"
        try:
            async with websockets_connect(url) as ws:
                self._dirty.clear()
                logger.info("启动感知 5m K线监听中: %s 个标的", len(pool))
                while not self._stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        if self._dirty.is_set():
                            break  # 小池更新 → 重连
                        continue
                    self._handle(msg)
        except Exception as exc:  # noqa: BLE001 - 交给外层重连
            if not self._stop.is_set():
                raise

    def _handle(self, msg: str) -> None:
        try:
            data = json.loads(msg)
            k = data.get("k") or {}
            if not k.get("x"):
                return  # 仅 K 线收盘事件
            mid = k.get("s") or ""
            symbol = self._to_symbol(mid)
            if symbol:
                self._on_close(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动感知 WS 消息解析失败: %s", exc)

    # ---------- K 线收盘评估 ----------

    def _on_close(self, symbol: str) -> None:
        """5m K 线收盘：拉最新数据评估 L3，全过 → 出信号。"""
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
        except Exception as exc:  # noqa: BLE001 - 单币失败不影响监听
            logger.warning("启动感知评估失败 %s: %s", symbol, exc)

    def _should_emit(self, symbol: str) -> bool:
        pat = '%"strategy": "launch_sense"%'
        if db.has_active_signal(symbol, "long", pat):
            return False
        if db.recent_closed_within(symbol, "long", pat, config.SIGNAL_COOLDOWN_MINUTES * 60_000):
            return False
        return True

    def _build_card(self, symbol: str, res: dict):
        """构造橙色启动感知信号卡（不打分，levels_detail 携带各层判定）。"""
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

    # ---------- symbol / market_id 转换 ----------

    def _market_id(self, symbol: str) -> str:
        return self.fetcher.exchange.market(symbol)["id"]

    def _to_symbol(self, market_id: str) -> Optional[str]:
        try:
            m = self.fetcher.exchange.markets_by_id.get(market_id)
            return m["symbol"] if m else None
        except Exception:  # noqa: BLE001
            return None


def websockets_connect(url: str):
    """websockets 连接（带代理支持，websockets>=11）。"""
    import websockets

    kwargs = {"ping_interval": 20, "max_size": 2 ** 20}
    if config.PROXY:
        kwargs["proxy"] = config.PROXY
    return websockets.connect(url, **kwargs)
