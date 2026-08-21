"""通知服务：WebSocket 连接管理 + 事件广播（TECH_DESIGN.md §4.7）。

事件协议（服务器→客户端）：
  signal:new    → SignalCard dict
  signal:expired→ {signal_id}
  alert:new     → 预警对象
  scan:report   → 扫描报告
  status:update → 系统状态

线程安全：调度器线程（非 async）通过 run_coroutine_threadsafe 广播。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """维护所有 WS 连接，支持从任意线程广播。"""

    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定主事件循环（FastAPI startup 时调用）。"""
        self._loop = loop

    # ---------- 连接管理 ----------

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        with self._lock:
            self._conns.add(ws)
        logger.info("WS 客户端接入，当前连接数 %s", len(self._conns))

    def disconnect(self, ws: WebSocket) -> None:
        with self._lock:
            self._conns.discard(ws)

    @property
    def count(self) -> int:
        return len(self._conns)

    # ---------- 广播 ----------

    def broadcast(self, event: str, payload: dict) -> None:
        """任意线程调用：提交协程到主事件循环广播。"""
        if self._loop is None or not self._conns:
            return
        asyncio.run_coroutine_threadsafe(self._send_all(event, payload), self._loop)

    async def _send_all(self, event: str, payload: dict) -> None:
        message = {"event": event, "data": payload}
        dead: list[WebSocket] = []
        for ws in list(self._conns):
            try:
                await ws.send_json(message)
            except Exception as exc:  # noqa: BLE001 - 客户端断开等异常
                logger.debug("WS 发送失败（%s）: %s", event, exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# 全局单例：API 与调度器共用
manager = ConnectionManager()
