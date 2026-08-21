"""活跃信号高频监控（产品文档 v4 §4.9 生命周期层）。

信号生成后进入监控队列，每 SIGNAL_MONITOR_INTERVAL_SEC（60s）轮询一次：
- 拉最新 5m K 线（绕过 300s 缓存）更新 live_price，实时反映信号标的行情
- 状态机：pending_confirm/confirmed → expired（超过有效期）| stopped_out（跌破止损）

变更信号通过 on_update 回调广播（signal:update 事件），前端实时刷新信号卡。
"""
import logging
import time
from typing import Callable, Optional

from ..store import db

logger = logging.getLogger(__name__)

# 监控中的信号状态（其余状态不再跟踪）
_MONITORED_STATUSES = ("pending_confirm", "confirmed")
# 实时价变化超过 0.01% 才视为更新（避免无意义刷新）
_PRICE_CHANGE_RATIO = 0.0001


class SignalMonitor:
    """活跃信号跟踪器：高频更新实时价，判定信号失效。"""

    def __init__(self, fetcher) -> None:
        self.fetcher = fetcher
        self.last_check_ts = 0
        self.on_update: Optional[Callable[[list[dict]], None]] = None

    def set_on_update(self, cb: Callable[[list[dict]], None]) -> None:
        """设置变更回调（调度线程调用，内部需线程安全）。"""
        self.on_update = cb

    def check(self) -> list[dict]:
        """轮询一轮：更新所有活跃信号，返回本轮发生变更的信号列表。"""
        changed = []
        for sig in db.get_active_signals():
            if sig.get("status") not in _MONITORED_STATUSES:
                continue
            updated = self._update(sig)
            if updated is not None:
                changed.append(updated)
        if changed and self.on_update:
            self.on_update(changed)
        self.last_check_ts = int(time.time() * 1000)
        return changed

    # ---------- 单信号更新 ----------

    def _update(self, sig: dict) -> Optional[dict]:
        now_ms = int(time.time() * 1000)
        # ① 超时 → expired（3 根 15m K 线）
        if now_ms >= sig.get("expires_at", 0):
            return self._save(sig, status="expired")

        # ② 拉最新 5m K 线（绕过缓存，拿到最新收盘价；limit=2 轻量化：权重 1、传输 2 根）
        # 缓存 key 含 limit 维度（ohlcv:sym:5m:2），不会污染精扫的 300 根缓存
        try:
            df = self.fetcher.fetch_ohlcv(sig["symbol"], "5m", use_cache=False, limit=2)
            if df is None or len(df) == 0:
                return None
            live = float(df["close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001 - 单信号失败不影响其他信号
            logger.warning("信号监控拉取失败 %s: %s", sig["symbol"], exc)
            return None

        # ③ 触及止损 → stopped_out（用收盘价判定，规避插针误判）
        stop = sig.get("execution", {}).get("stop_loss")
        if stop:
            hit = (
                (sig["direction"] == "long" and live <= stop)
                or (sig["direction"] == "short" and live >= stop)
            )
            if hit:
                return self._save(sig, status="stopped_out", live=live)

        # ④ 正常：仅更新实时价，保持原状态（pending_confirm/confirmed 均视为有效，
        # 确认逻辑由精扫收盘确认负责，monitor 只管生命周期失效判定）
        old = sig.get("live_price")
        if old is None or abs(live - old) / old > _PRICE_CHANGE_RATIO:
            return self._save(sig, status=sig.get("status") or "pending_confirm", live=live)
        return None

    def _save(self, sig: dict, status: str, live: Optional[float] = None) -> dict:
        """更新状态/实时价并落库（INSERT OR REPLACE 全量覆盖）。"""
        sig["status"] = status
        if live is not None:
            sig["live_price"] = live
            sig["live_updated_at"] = int(time.time() * 1000)
        db.save_signal(sig)
        return sig
