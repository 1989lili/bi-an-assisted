"""活跃信号高频监控（产品文档 v4 §4.9 生命周期层）。

信号生成后进入监控队列，每 SIGNAL_MONITOR_INTERVAL_SEC（60s）轮询一次：
- 拉最新 5m K 线（绕过 300s 缓存）更新 live_price，实时反映信号标的行情
- 状态机：pending_confirm/confirmed → expired（超过有效期）| stopped_out（跌破止损）

变更信号通过 on_update 回调广播（signal:update 事件），前端实时刷新信号卡。
"""
import logging
import time
from typing import Callable, Optional

from .. import config
from ..store import db

logger = logging.getLogger(__name__)

# 监控中的信号状态（其余状态不再跟踪）
_MONITORED_STATUSES = ("pending_confirm", "confirmed")
# 实时价变化超过 0.01% 才视为更新（避免无意义刷新）
_PRICE_CHANGE_RATIO = 0.0001


def _entry_bar_ms() -> int:
    """策略一入场周期单根 K 线毫秒数（时间止损用）。"""
    tf = config.EMA_TREND_TIMEFRAMES["entry"]
    minutes = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}.get(tf, 15)
    return minutes * 60 * 1000


def _exit_type_from_reason(reason: str) -> str:
    """离场原因 → 结局类型：stop(止损)/trend(趋势失效)/time(时间止损)/expired(过期)。"""
    r = reason or ""
    if "时间" in r:
        return "time"
    if "吊灯" in r or "止损" in r:
        return "stop"
    if "EMA50" in r or "趋势" in r:
        return "trend"
    return "trend"


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
        # 策略一（EMA 趋势跟踪）：走三层出场判定（吊灯 / EMA50 / 时间止损）
        if sig.get("strategy") == "ema_trend":
            return self._update_ema_trend(sig, now_ms)
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

        # 记录是否曾到达第一目标（兑现率统计用）
        target = sig.get("execution", {}).get("target")
        if target:
            if (sig["direction"] == "long" and live >= target) or (sig["direction"] == "short" and live <= target):
                sig["hit_target"] = True

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

    # ---------- 策略一（EMA 趋势跟踪）出场判定 ----------

    def _update_ema_trend(self, sig: dict, now_ms: int) -> Optional[dict]:
        """拉入场周期 K 线 → 更新持仓期最高/最低收盘 → 三层出场（吊灯 / EMA50 / 时间止损）。"""
        entry_key = config.EMA_TREND_TIMEFRAMES["entry"]
        try:
            df = self.fetcher.fetch_ohlcv(sig["symbol"], entry_key, use_cache=False, limit=60)
            if df is None or len(df) < 20:
                return None
            last_close = float(df["close"].iloc[-1])
        except Exception as exc:  # noqa: BLE001 - 单信号失败不影响其他信号
            logger.warning("策略一监控拉取失败 %s: %s", sig["symbol"], exc)
            return None

        exec_ = sig.setdefault("execution", {})
        prev_high = exec_.get("highest_close") or last_close
        prev_low = exec_.get("lowest_close") or last_close
        exec_["highest_close"] = max(prev_high, last_close)
        exec_["lowest_close"] = min(prev_low, last_close)

        # 记录是否曾到达第一目标（兑现率统计用）
        target = exec_.get("target")
        if target:
            if (sig["direction"] == "long" and exec_["highest_close"] >= target) or \
               (sig["direction"] == "short" and exec_["lowest_close"] <= target):
                sig["hit_target"] = True

        bar_ms = _entry_bar_ms()
        elapsed = int((now_ms - sig.get("created_at", now_ms)) / bar_ms) if bar_ms else None

        from ..strategy.ema_trend import check_exit

        reason = check_exit(sig, df, elapsed_bars=elapsed)
        if reason:
            sig["reason"] = f"{sig.get('reason', '')}｜离场：{reason}"
            return self._save(sig, status="stopped_out", live=last_close)
        return self._save(sig, status=sig.get("status") or "confirmed", live=last_close)

    def _save(self, sig: dict, status: str, live: Optional[float] = None) -> dict:
        """更新状态/实时价并落库。离场时记录结局（exit_type/hit_target，供兑现率统计）。"""
        sig["status"] = status
        if live is not None:
            sig["live_price"] = live
            sig["live_updated_at"] = int(time.time() * 1000)
        if status in ("stopped_out", "expired") and "result" not in sig:
            sig["result"] = {
                "exit_type": "expired" if status == "expired" else _exit_type_from_reason(sig.get("reason", "")),
                "exit_price": live if live is not None else sig.get("live_price"),
                "hit_target": bool(sig.get("hit_target")),
            }
        db.save_signal(sig)
        return sig
