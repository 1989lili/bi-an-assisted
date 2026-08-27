"""持仓自动风控（N1.3）：轮询 open 持仓，按对应出场规则自动平仓。

- 短线（strategy='short'）：止损三段式（position.manager.evaluate_stage）→ action=='exit' 即平仓。
- 策略一（strategy='ema_trend'）：三层出场（strategy.ema_trend.check_exit）。
- 平仓为反向市价单（dry_run 下仅模拟），写入已实现盈亏、更新关联信号状态。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .. import config
from ..calendar.macro import in_silence_window
from ..indicators.engine import atr, ema
from ..signal.monitor import _exit_type_from_reason
from ..store import db

logger = logging.getLogger(__name__)


def _entry_bar_ms() -> int:
    """策略一入场周期单根 K 线毫秒数（时间止损用）。"""
    tf = config.EMA_TREND_TIMEFRAMES["entry"]
    minutes = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}.get(tf, 15)
    return minutes * 60 * 1000


def _elapsed_bars(pos: dict) -> Optional[int]:
    """持仓已过入场周期 K 线数（M1：策略一时间止损对持仓生效）。"""
    opened = pos.get("opened_at")
    if not opened:
        return None
    try:
        opened_ms = int(datetime.fromisoformat(opened).timestamp() * 1000)
    except (ValueError, TypeError):
        return None
    bar_ms = _entry_bar_ms()
    now_ms = int(time.time() * 1000)
    return int((now_ms - opened_ms) / bar_ms) if bar_ms else None


class PositionMonitor:
    def __init__(self, fetcher, executor) -> None:
        self.fetcher = fetcher
        self.executor = executor
        self.on_update: Optional[Callable[..., None]] = None

    def set_on_update(self, cb) -> None:
        self.on_update = cb

    def check(self) -> list[dict]:
        changed = []
        # 宏观静默窗口（每轮判定一次）：窗口内对未保护持仓执行保护（减仓/收紧止损）
        silent = in_silence_window()
        for pos in db.get_positions("open"):
            try:
                outcome = self._evaluate(pos)
            except Exception as exc:  # noqa: BLE001 - 单持仓失败不影响其他
                logger.warning("持仓评估失败 %s: %s", pos.get("symbol"), exc)
                continue
            if outcome:
                self._close(pos, outcome)
                changed.append({"id": pos["id"], **outcome})
                continue
            if silent:
                try:
                    self._macro_protect(pos)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("宏观静默保护失败 %s: %s", pos.get("symbol"), exc)
        if changed and self.on_update:
            self.on_update(changed)
        return changed

    def _evaluate(self, pos: dict) -> Optional[dict]:
        df15 = self.fetcher.fetch_ohlcv(pos["symbol"], config.TIMEFRAMES["entry"], limit=60)
        if df15 is None or len(df15) < 20:
            return None
        price = float(df15["close"].iloc[-1])

        # ---------- 策略一：三层出场 ----------
        if pos.get("strategy") == "ema_trend":
            sig = db.get_signal(pos.get("signal_id")) if pos.get("signal_id") else None
            if not sig:
                return None
            exec_ = sig.setdefault("execution", {})
            exec_["highest_close"] = max(float(exec_.get("highest_close") or price), price)
            exec_["lowest_close"] = min(float(exec_.get("lowest_close") or price), price)
            db.save_signal(sig)
            from ..strategy.ema_trend import check_exit

            # M1：时间止损按持仓时长计算（否则 48 根未创新高对持仓永不触发）
            reason = check_exit(sig, df15, elapsed_bars=_elapsed_bars(pos))
            if reason:
                return {"exit": True, "reason": reason, "price": price, "signal_id": pos.get("signal_id")}
            return None

        # ---------- 短线：止损三段式 ----------
        atr_v = float(atr(df15).iloc[-1])
        df1h = self.fetcher.fetch_ohlcv(pos["symbol"], config.TIMEFRAMES["confirm"], limit=80)
        ema21_1h = float(ema(df1h["close"], 21).iloc[-1]) if df1h is not None and len(df1h) > 25 else None
        from ..position.manager import evaluate_stage

        res = evaluate_stage(pos, price, atr_v, ema21_1h)
        action = res.get("action")
        if action == "exit":
            return {"exit": True, "reason": res.get("reason"), "price": price, "signal_id": pos.get("signal_id")}
        if action == "move_stop":
            # H2：保本/跟踪升级持久化（新止损价 + 阶段写回 DB，避免三段式升级失效）
            db.update_position(
                pos["id"],
                stop_price=res.get("stop_price"),
                stop_stage=res.get("stage", pos.get("stop_stage", 1)),
            )
        return None

    def _macro_protect(self, pos: dict) -> None:
        """宏观静默窗口内保护已持仓（防插针，每持仓仅执行一次）：

        1) 可选减仓：`MACRO_SILENCE_REDUCE_PCT > 0` 时按比例 reduceOnly 部分平仓
           （剩余不足币种最小下单量则放弃减仓，仅收紧止损）。
        2) 收紧止损：移到现价 ± `MACRO_SILENCE_STOP_ATR`×ATR 处（只紧不松，绝不放大风险），
           并同步交易所侧 STOP_MARKET 单（撤旧单按新数量/新价重挂）。
        """
        if int(pos.get("macro_protected") or 0):
            return
        df15 = self.fetcher.fetch_ohlcv(pos["symbol"], config.TIMEFRAMES["entry"], limit=60)
        if df15 is None or len(df15) < 20:
            return
        price = float(df15["close"].iloc[-1])
        atr15 = float(atr(df15).iloc[-1])
        direction = pos["direction"]
        qty = float(pos.get("qty") or 0)
        side = "sell" if direction == "long" else "buy"

        # 1) 可选减仓
        reduce_pct = float(config.MACRO_SILENCE_REDUCE_PCT or 0)
        if reduce_pct > 0 and qty > 0 and self.executor.configured:
            reduce_qty = self.executor.amount_to_precision(pos["symbol"], qty * reduce_pct)
            remain = round(qty - reduce_qty, 8)
            min_qty = self.executor.min_amount(pos["symbol"]) or 0
            if reduce_qty > 0 and remain >= min_qty:
                order = self.executor.create_order(pos["symbol"], side, reduce_qty,
                                                   order_type="market", reduce_only=True)
                if order.get("ok"):
                    db.update_position(pos["id"], qty=remain)
                    qty = remain
                    logger.info("宏观静默减仓 %s %s %s→%s (order=%s)",
                                pos["symbol"], direction, round(remain + reduce_qty, 8), remain,
                                order.get("id"))

        # 2) 收紧止损（只紧不松）
        buf = config.MACRO_SILENCE_STOP_ATR * atr15
        new_stop = price - buf if direction == "long" else price + buf
        old_stop = pos.get("stop_price")
        if old_stop is not None:
            if direction == "long":
                new_stop = max(float(old_stop), new_stop)
            else:
                new_stop = min(float(old_stop), new_stop)
        new_stop = round(new_stop, 8)

        # 交易所侧止损单同步：撤旧单，按（可能已减仓的）新数量挂新单
        new_stop_id = pos.get("stop_order_id")
        if new_stop_id and self.executor.configured:
            self.executor.cancel_order(pos["symbol"], new_stop_id)
            stop_order = self.executor.create_stop_loss_order(pos["symbol"], side, qty, new_stop)
            new_stop_id = stop_order.get("id") if stop_order.get("ok") else None
        db.update_position(pos["id"], stop_price=new_stop, macro_protected=1,
                           stop_order_id=new_stop_id or None)
        logger.info("宏观静默收紧止损 %s %s: %s → %s (atr=%.6g)",
                    pos["symbol"], direction, old_stop, new_stop, atr15)

    def _close(self, pos: dict, outcome: dict) -> None:
        if not self.executor.configured:
            logger.warning("未配置币安凭据，无法自动平仓 %s", pos.get("symbol"))
            return
        # H7：平仓前先撤交易所侧止损单
        stop_order_id = pos.get("stop_order_id")
        if stop_order_id:
            self.executor.cancel_order(pos["symbol"], stop_order_id)
        side = "sell" if pos["direction"] == "long" else "buy"
        order = self.executor.create_order(pos["symbol"], side, float(pos.get("qty") or 0),
                                           order_type="market", reduce_only=True)
        if not order.get("ok"):
            # H4：下单失败不回滚为 closed（保持 open，下轮重试）
            logger.error("自动平仓下单失败 %s %s: %s", pos["symbol"], pos["direction"], order.get("error"))
            return
        price = float(outcome.get("price") or pos.get("entry_price") or 0)
        entry = float(pos.get("entry_price") or 0)
        qty = float(pos.get("qty") or 0)
        pnl = (price - entry) * qty if pos["direction"] == "long" else (entry - price) * qty
        closed_at = datetime.now(timezone.utc).isoformat()
        db.update_position(pos["id"], status="closed", realized_pnl=round(pnl, 8), closed_at=closed_at)
        logger.info("自动平仓 %s %s @%s pnl=%.4f (%s); order=%s",
                    pos["symbol"], pos["direction"], price, pnl, outcome.get("reason"), order.get("id"))

        sid = pos.get("signal_id")
        if sid:
            sig = db.get_signal(sid)
            if sig:
                sig["status"] = "stopped_out"
                sig["closed_price"] = price
                sig["realized_pnl"] = round(pnl, 8)
                sig["reason"] = f"{sig.get('reason', '')}｜离场：{outcome.get('reason')}"
                # 结果跟踪：结局类型 + 是否曾到第一目标（兑现率统计）
                exec_ = sig.setdefault("execution", {})
                target = exec_.get("target")
                hit_target = bool(sig.get("hit_target"))
                if target:
                    if (pos["direction"] == "long" and float(exec_.get("highest_close") or price) >= target) or \
                       (pos["direction"] == "short" and float(exec_.get("lowest_close") or price) <= target):
                        hit_target = True
                sig["result"] = {
                    "exit_type": _exit_type_from_reason(outcome.get("reason", "")),
                    "exit_price": price,
                    "hit_target": hit_target,
                }
                db.save_signal(sig)
