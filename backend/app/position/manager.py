"""持仓管理器：止损三段式状态机（产品文档 v4 §4-8️⃣，TECH_DESIGN.md §4.4）。

阶段一（初始）：止损 = 波动率系数×ATR
阶段二（保本）：浮盈 ≥1.5×ATR → 止损移至入场价
阶段三（跟踪）：浮盈 ≥3×ATR → EMA21 动态跟踪，1h 收盘跌破离场

纯函数设计，便于单元测试；持仓落库与 UI 接入在 API 层。
"""
from __future__ import annotations

from .. import config


def initial_stop(entry_price: float, direction: str, coef: float, atr: float) -> float:
    """阶段一：初始止损 = coef×ATR（coef 为波动率自适应系数 1.0/1.5/2.0）。"""
    dist = coef * atr
    return entry_price - dist if direction == "long" else entry_price + dist


def evaluate_stage(
    position: dict,
    current_price: float,
    atr: float,
    ema21_1h: float | None = None,
) -> dict:
    """评估持仓止损阶段，返回 {stage, stop_price, action, reason}。

    position: {direction, entry_price, qty, stage, stop_price}
    """
    direction = position["direction"]
    entry = position["entry_price"]
    # 兼容 db 字段名 stop_stage（避免阶段永远停在初始、三段式升级失效）
    stage = position.get("stage", position.get("stop_stage", 1))
    stop = position.get("stop_price")

    # 浮盈（ATR 单位）
    if direction == "long":
        profit_atr = (current_price - entry) / atr if atr else 0.0
    else:
        profit_atr = (entry - current_price) / atr if atr else 0.0

    # ---------- 阶段一 → 二：浮盈 ≥1.5×ATR 保本 ----------
    if stage == 1 and profit_atr >= config.BE_PROFIT_ATR:
        stop = entry  # 保本止损
        return {
            "stage": 2,
            "stop_price": stop,
            "action": "move_stop",
            "reason": f"浮盈 {profit_atr:.2f}×ATR ≥ {config.BE_PROFIT_ATR}，止损移至成本价保本",
        }

    # ---------- 阶段二 → 三：浮盈 ≥3×ATR 转 EMA21 跟踪 ----------
    if stage == 2 and profit_atr >= config.TRAIL_PROFIT_ATR and ema21_1h is not None:
        stop = ema21_1h
        return {
            "stage": 3,
            "stop_price": stop,
            "action": "move_stop",
            "reason": f"浮盈 {profit_atr:.2f}×ATR ≥ {config.TRAIL_PROFIT_ATR}，止损上移至 1h EMA21 跟踪",
        }

    # ---------- 阶段三：EMA21 动态上移（只升不降） ----------
    if stage == 3 and ema21_1h is not None:
        if direction == "long" and ema21_1h > (stop or 0):
            return {
                "stage": 3,
                "stop_price": ema21_1h,
                "action": "move_stop",
                "reason": f"1h EMA21 上移，止损跟随至 {ema21_1h:.4g}",
            }
        if direction == "short" and ema21_1h < (stop or float("inf")):
            return {
                "stage": 3,
                "stop_price": ema21_1h,
                "action": "move_stop",
                "reason": f"1h EMA21 下移，止损跟随至 {ema21_1h:.4g}",
            }

    # ---------- 离场判定：1h 收盘跌破 EMA21（阶段三）或跌破当前止损 ----------
    if stage == 3 and ema21_1h is not None:
        if direction == "long" and current_price < ema21_1h:
            return {
                "stage": stage,
                "stop_price": stop,
                "action": "exit",
                "reason": f"1h 收盘跌破 EMA21（{ema21_1h:.4g}），按跟踪止损离场",
            }
        if direction == "short" and current_price > ema21_1h:
            return {
                "stage": stage,
                "stop_price": stop,
                "action": "exit",
                "reason": f"1h 收盘站上 EMA21（{ema21_1h:.4g}），按跟踪止损离场",
            }
    if stop is not None:
        if direction == "long" and current_price <= stop:
            return {"stage": stage, "stop_price": stop, "action": "exit", "reason": "价格触及止损"}
        if direction == "short" and current_price >= stop:
            return {"stage": stage, "stop_price": stop, "action": "exit", "reason": "价格触及止损"}

    return {"stage": stage, "stop_price": stop, "action": "hold", "reason": "无动作"}


def position_snapshot(position: dict, current_price: float, atr: float, ema21_1h: float | None = None) -> dict:
    """持仓快照（API 输出用）：阶段/止损/浮盈/建议动作。"""
    result = evaluate_stage(position, current_price, atr, ema21_1h)
    direction = position["direction"]
    entry = position["entry_price"]
    if direction == "long":
        pnl_pct = (current_price - entry) / entry
    else:
        pnl_pct = (entry - current_price) / entry
    return {
        **position,
        "stage": result["stage"],
        "stop_price": result["stop_price"],
        "action": result["action"],
        "action_reason": result["reason"],
        "pnl_pct": round(pnl_pct * 100, 3),
        "price": current_price,
    }
