"""持仓自动风控（N1.3）：轮询 open 持仓，按对应出场规则自动平仓。

- 短线（strategy='short'）：止损三段式（position.manager.evaluate_stage）→ action=='exit' 即平仓。
- 策略一（strategy='ema_trend'）：三层出场（strategy.ema_trend.check_exit）。
- 平仓为反向市价单（dry_run 下仅模拟），写入已实现盈亏、更新关联信号状态。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from .. import config
from ..indicators.engine import atr, ema
from ..store import db

logger = logging.getLogger(__name__)


class PositionMonitor:
    def __init__(self, fetcher, executor) -> None:
        self.fetcher = fetcher
        self.executor = executor
        self.on_update: Optional[Callable[..., None]] = None

    def set_on_update(self, cb) -> None:
        self.on_update = cb

    def check(self) -> list[dict]:
        changed = []
        for pos in db.get_positions("open"):
            try:
                outcome = self._evaluate(pos)
            except Exception as exc:  # noqa: BLE001 - 单持仓失败不影响其他
                logger.warning("持仓评估失败 %s: %s", pos.get("symbol"), exc)
                continue
            if outcome:
                self._close(pos, outcome)
                changed.append({"id": pos["id"], **outcome})
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

            reason = check_exit(sig, df15)
            if reason:
                return {"exit": True, "reason": reason, "price": price, "signal_id": pos.get("signal_id")}
            return None

        # ---------- 短线：止损三段式 ----------
        atr_v = float(atr(df15).iloc[-1])
        df1h = self.fetcher.fetch_ohlcv(pos["symbol"], config.TIMEFRAMES["confirm"], limit=80)
        ema21_1h = float(ema(df1h["close"], 21).iloc[-1]) if df1h is not None and len(df1h) > 25 else None
        from ..position.manager import evaluate_stage

        res = evaluate_stage(pos, price, atr_v, ema21_1h)
        if res.get("action") == "exit":
            return {"exit": True, "reason": res.get("reason"), "price": price, "signal_id": pos.get("signal_id")}
        return None

    def _close(self, pos: dict, outcome: dict) -> None:
        if not self.executor.configured:
            logger.warning("未配置币安凭据，无法自动平仓 %s", pos.get("symbol"))
            return
        side = "sell" if pos["direction"] == "long" else "buy"
        order = self.executor.create_order(pos["symbol"], side, float(pos.get("qty") or 0), order_type="market")
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
                db.save_signal(sig)
