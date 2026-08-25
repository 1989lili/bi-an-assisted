"""REST 路由 + WebSocket 端点（TECH_DESIGN.md §4.7 / §5）。

共享实例通过 app.state 注入（main.py startup 时设置），便于测试。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .. import config
from ..notify.ws import manager
from ..position.manager import initial_stop, position_snapshot
from ..store import db

router = APIRouter(prefix="/api")


# ==================== 数据模型 ====================


class PositionCreate(BaseModel):
    symbol: str
    direction: str = Field(pattern="^(long|short)$")
    entry_price: float = Field(gt=0)
    qty: float = Field(gt=0)
    stop_price: Optional[float] = None


class PositionPatch(BaseModel):
    stop_price: Optional[float] = None
    stop_stage: Optional[int] = Field(default=None, ge=1, le=3)


class MacroEventCreate(BaseModel):
    title: str
    event_time: str  # ISO 格式，如 2026-09-10T20:30:00+08:00


class SettingValue(BaseModel):
    value: Any


# ==================== 系统状态 ====================


@router.get("/status")
def status(request: Request) -> dict:
    deep = request.app.state.deep
    sched = request.app.state.scheduler
    jobs = []
    for job in sched.get_jobs():
        jobs.append({"id": job.id, "next_run": str(job.next_run_time) if job.next_run_time else None})
    return {
        "version": "0.3.0",
        "scheduler_running": sched.running,
        "jobs": jobs,
        "ws_connections": manager.count,
        "candidate_count": len(getattr(deep, "last_pool", []) or []),
        "candidates": list(getattr(deep, "last_pool", []) or []),
        "rejections": dict(getattr(getattr(deep, "engine", None), "rejections", {}) or {}),
        "last_scan_ts": getattr(deep, "last_scan_ts", None),
        "market_env": getattr(deep, "last_market_env", None),
        "proxy": config.PROXY,
    }


# ==================== 信号 ====================


@router.get("/signals")
def list_signals(symbol: Optional[str] = None, status: Optional[str] = None, limit: int = 50) -> list[dict]:
    return db.get_signals(limit=min(max(limit, 1), 200), symbol=symbol, status=status)


@router.get("/signals/{signal_id}")
def get_signal(signal_id: str) -> dict:
    sig = db.get_signal(signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail="信号不存在")
    return sig


@router.post("/signals/{signal_id}/execute")
def execute_signal(signal_id: str, request: Request) -> dict:
    """一键执行信号：按信号卡执行计划下单（dry_run 下仅模拟）并创建本地持仓。

    幂等：已执行过的信号 id 拒绝重复下单。
    """
    sig = db.get_signal(signal_id)
    if sig is None:
        raise HTTPException(status_code=404, detail="信号不存在")
    if sig.get("executed"):
        raise HTTPException(status_code=409, detail="信号已执行，请勿重复")
    if sig.get("status") not in ("confirmed", "pending_confirm"):
        raise HTTPException(status_code=400, detail=f"信号状态 {sig.get('status')} 不可执行")

    executor = request.app.state.executor
    if not executor.configured:
        raise HTTPException(status_code=503, detail="未配置币安 API Key/Secret")

    exec_plan = sig.get("execution") or {}
    symbol = sig["symbol"]
    direction = sig["direction"]
    price = float(exec_plan.get("market_price") or 0)
    if price <= 0:
        raise HTTPException(status_code=400, detail="信号缺少执行价格")

    bal = executor.fetch_balance()
    if not bal.get("ok"):
        raise HTTPException(status_code=503, detail=f"余额查询失败: {bal.get('error')}")
    free = float(bal.get("free") or 0)
    budget = min(free, config.BINANCE_MAX_ORDER_USDT)  # 单笔预算上限
    amount = round(budget / price, 6)                  # 合约数量（第一版：预算/价格）
    if amount <= 0:
        raise HTTPException(status_code=400, detail="可用余额不足以开仓")

    side = "buy" if direction == "long" else "sell"
    market = executor.create_order(symbol, side, amount, order_type="market")
    if not market.get("ok"):
        raise HTTPException(status_code=502, detail=f"下单失败: {market.get('error')}")

    pid = db.create_position(symbol, direction, price, amount,
                             stop_price=exec_plan.get("stop_loss") or None, stop_stage=1)
    sig["executed"] = True
    sig["executed_at"] = int(time.time() * 1000)
    sig["exec_side"] = side
    sig["exec_amount"] = amount
    db.save_signal(sig)

    return {
        "ok": True, "dry_run": market.get("dry_run"), "signal_id": signal_id,
        "position_id": pid, "side": side, "amount": amount,
        "order_id": market.get("id"), "price": price,
        "budget_usdt": budget, "mode": executor.mode_label,
    }


# ==================== 自选币 ====================


@router.get("/watchlist")
def get_watchlist() -> dict:
    return {"symbols": sorted(db.get_watchlist())}


@router.post("/watchlist")
def add_watch(symbol: str = Body(..., embed=True)) -> dict:
    if "/" not in symbol or ":" not in symbol:
        raise HTTPException(status_code=400, detail="symbol 需为 ccxt 格式，如 BTC/USDT:USDT")
    db.add_watch(symbol)
    return {"ok": True, "symbol": symbol}


@router.delete("/watchlist")
def remove_watch(symbol: str) -> dict:
    """删除自选（查询参数传 symbol，避免路径中斜杠冲突）。"""
    db.remove_watch(symbol)
    return {"ok": True, "symbol": symbol}


# ==================== 持仓 ====================


@router.get("/positions")
def list_positions(status: str = "open") -> list[dict]:
    return db.get_positions(status)


@router.post("/positions")
def create_position(request: Request, payload: PositionCreate) -> dict:
    """创建持仓；未手动指定止损时自动计算初始止损（SL_INIT_COEF × 15m ATR）。"""
    stop_price = payload.stop_price
    if stop_price is None:
        try:
            df15 = request.app.state.fetcher.fetch_ohlcv(payload.symbol, config.TIMEFRAMES["entry"])
            if df15 is not None and len(df15) >= config.ATR_PERIOD + 1:
                from ..indicators.engine import atr

                atr_val = float(atr(df15).iloc[-1])
                stop_price = initial_stop(payload.entry_price, payload.direction, config.SL_INIT_COEF, atr_val)
        except Exception:  # noqa: BLE001 行情不可用时暂不设止损，不阻塞创建
            pass
    pid = db.create_position(
        payload.symbol, payload.direction, payload.entry_price, payload.qty,
        stop_price=stop_price,
    )
    return {"ok": True, "id": pid}


@router.patch("/positions/{position_id}")
def patch_position(position_id: int, payload: PositionPatch) -> dict:
    fields = payload.model_dump(exclude_none=True)
    if not db.update_position(position_id, **fields):
        raise HTTPException(status_code=404, detail="持仓不存在")
    return {"ok": True}


@router.post("/positions/{position_id}/close")
def close_position(position_id: int) -> dict:
    if not db.close_position(position_id):
        raise HTTPException(status_code=404, detail="持仓不存在或已平仓")
    return {"ok": True}


@router.get("/positions/{position_id}/status")
def position_status(request: Request, position_id: int) -> dict:
    """实时评估：拉 15m ATR + 1h EMA21 → 止损阶段/建议动作。"""
    position = db.get_position(position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="持仓不存在")
    fetcher = request.app.state.fetcher
    df15 = fetcher.fetch_ohlcv(position["symbol"], config.TIMEFRAMES["entry"])
    df1h = fetcher.fetch_ohlcv(position["symbol"], config.TIMEFRAMES["confirm"])
    if df15 is None or df1h is None:
        raise HTTPException(status_code=503, detail="行情暂不可用，稍后重试")
    from ..indicators.engine import atr, ema

    atr_val = float(atr(df15).iloc[-1])
    ema21_1h = float(ema(df1h["close"], 21).iloc[-1])
    price = float(df15["close"].iloc[-1])
    snap = position_snapshot(position, price, atr_val, ema21_1h)
    return {"symbol": position["symbol"], "price": price, "atr": round(atr_val, 8),
            "ema21_1h": round(ema21_1h, 8), **snap}


# ==================== 宏观日历 ====================


@router.get("/macro-events")
def list_macro_events() -> list[dict]:
    return db.get_macro_events()


@router.post("/macro-events")
def add_macro_event(payload: MacroEventCreate) -> dict:
    db.add_macro_event(payload.title, payload.event_time)
    return {"ok": True}


@router.delete("/macro-events/{event_id}")
def remove_macro_event(event_id: int) -> dict:
    db.remove_macro_event(event_id)
    return {"ok": True}


# ==================== 设置（运行时覆盖，M5 持久化） ====================


@router.get("/settings")
def get_settings() -> dict:
    """返回全部配置参数（config 模块大写常量）。敏感项（API Key/Secret）不外泄。"""
    _sensitive = {"BINANCE_API_KEY", "BINANCE_API_SECRET"}
    return {
        k: v for k, v in vars(config).items()
        if k.isupper() and not k.startswith("_") and k not in _sensitive
    }


@router.put("/settings/{key}")
def update_setting(key: str, payload: SettingValue) -> dict:
    """运行时覆盖配置（仅影响内存，重启恢复默认）。key 必须是大写配置名。"""
    if not key.isupper() or not hasattr(config, key):
        raise HTTPException(status_code=400, detail="无效配置项")
    setattr(config, key, payload.value)
    db.set_setting(key, str(payload.value))
    config.save_setting(key, payload.value)  # 持久化到 data/settings.json（重启仍生效）
    return {"ok": True, "key": key, "value": payload.value}


# ==================== 单币指标快照（自选页） ====================


@router.get("/symbol/snapshot")
def symbol_snapshot(symbol: str, request: Request) -> dict:
    """单币指标快照：4 周期指标 + 费率档位 + OI 变化率 + 清算距离（自选页展开用）。

    symbol 经查询参数传递（路径参数无法携带含斜杠的币种格式）。
    """
    fetcher = request.app.state.fetcher
    klines = {}
    for tf in config.TIMEFRAMES.values():
        df = fetcher.fetch_ohlcv(symbol, tf)
        if df is None or df.empty:
            raise HTTPException(status_code=503, detail=f"{tf} 行情暂不可用")
        klines[tf] = df

    from ..indicators.engine import (
        compute_funding_tier,
        compute_indicator_snapshot,
        estimate_liquidity_zones,
        nearest_zone_distance,
        volatility_coef,
    )

    snap = compute_indicator_snapshot(klines)
    funding = compute_funding_tier(db.get_funding_history(symbol, hours=24))

    oi_change = None
    try:
        df_oi = fetcher.fetch_oi_history(symbol, timeframe="5m", limit=6)
        if df_oi is not None and len(df_oi) >= 2:
            first, last = float(df_oi["openInterest"].iloc[0]), float(df_oi["openInterest"].iloc[-1])
            if first > 0:
                oi_change = (last - first) / first
    except Exception:  # noqa: BLE001 - OI 历史不可用时降级
        pass

    zones = estimate_liquidity_zones(klines["15m"])
    s15 = snap.get("15m")
    liq_dist = None
    coef = None
    if s15:
        liq_dist = nearest_zone_distance(s15["close"], zones, "long")
        if liq_dist is None:
            liq_dist = nearest_zone_distance(s15["close"], zones, "short")
        coef = volatility_coef(s15["bw"], s15["bw_median"])
    return {
        "symbol": symbol,
        "snap": snap,
        "funding": funding,
        "oi_change": oi_change,
        "liq_dist": liq_dist,
        "volatility_coef": coef,
    }


# ==================== 扫描日志 ====================


@router.get("/scan-logs")
def list_scan_logs(limit: int = 100) -> list[dict]:
    return db.get_scan_logs(limit=min(max(limit, 1), 500))


# ==================== WebSocket ====================


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            # 客户端消息（watchlist/持仓操作）M4 处理，先保持连接
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:  # noqa: BLE001
        manager.disconnect(ws)
