"""REST 路由 + WebSocket 端点（TECH_DESIGN.md §4.7 / §5）。

共享实例通过 app.state 注入（main.py startup 时设置），便于测试。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .. import config
from ..notify.ws import manager
from ..position.manager import initial_stop, position_snapshot
from ..store import db


# H6 鉴权由 main.py 的 HTTP 中间件统一处理（WS 在端点内校验 query token）

# H6 白名单：允许运行时修改的策略参数（禁止 BINANCE_*/路径/鉴权等敏感项）
_SETTING_ALLOWLIST = frozenset({
    "ADX_TREND_TH", "TRIGGER_MOMENTUM_BARS",
    "VOL_RATIO_VETO", "VOL_RATIO_HOT", "VOL_RATIO_LOW", "OI_GROWTH_VETO",
    "VOL_SCORE_STRONG", "VOL_SCORE_MILD",
    "VOL_TARGET_ATR_PCT", "VOL_FACTOR_MIN", "VOL_FACTOR_MAX",
    "FUNDING_NORMAL_MAX", "FUNDING_STABLE_MAX", "FUNDING_SURGE_TIMES",
    "FUNDING_STABLE_FLUCT", "FUNDING_POSITION_FACTOR",
    "BW_NARROW_FACTOR", "BW_WIDE_FACTOR",
    "ATR_COEF_NARROW", "ATR_COEF_NORMAL", "ATR_COEF_WIDE",
    "MIN_RISK_REWARD", "RISK_PER_TRADE",
    "EXEC_MARKET_PCT", "EXEC_LIMIT_PCT", "EXEC_LIMIT_TTL_BARS",
    "SIGNAL_TTL_BARS", "SIGNAL_COOLDOWN_MINUTES",
    "SL_INIT_COEF", "BE_PROFIT_ATR", "TRAIL_PROFIT_ATR",
    "MACRO_SILENCE_MINUTES",
    "EMA_TREND_VOL_MULT", "EMA_TREND_RETRACE_LOOKBACK", "EMA_TREND_ENTRY_NEAR_ATR",
    "EMA_TREND_RSI_MIN", "EMA_TREND_RSI_MAX", "EMA_TREND_EXIT_ATR",
    "EMA_TREND_TP_RR", "EMA_TREND_TIME_BARS",
    "CANDIDATE_MIN_QUOTE_VOLUME", "CANDIDATE_TOP_VOLUME",
    "CANDIDATE_TOP_CHANGE", "CANDIDATE_TOP_GAIN",
})

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


class ExecuteRequest(BaseModel):
    """一键执行请求：budget_usdt 可选（用户确认页调整后的预算；缺省 = 总余额×EXEC_DEFAULT_BUDGET_PCT）。"""
    budget_usdt: Optional[float] = None


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


_execute_lock = threading.Lock()  # 进程内互斥：防并发重复执行同一信号


@router.get("/signal-stats")
def signal_stats() -> dict:
    """信号结局统计（评估模型准确性）：按策略统计止损/止盈(到目标)/趋势/时间离场 + 兑现率。"""
    sigs = db.get_signals(limit=500)
    by_strategy: dict[str, dict] = {}
    for s in sigs:
        strat = s.get("strategy") or "short"
        st = by_strategy.setdefault(strat, {
            "total": 0, "settled": 0, "pending": 0, "unknown": 0,
            "stop": 0, "trend": 0, "time": 0, "expired": 0, "target_hit": 0,
            "conf_sum": 0.0, "conf_count": 0,
        })
        st["total"] += 1
        if s.get("status") in ("stopped_out", "expired"):
            r = s.get("result")
            if not r:  # 历史信号无结局标注（结果跟踪上线前）
                st["unknown"] += 1
                continue
            st["settled"] += 1
            et = r.get("exit_type")
            if et == "stop":
                st["stop"] += 1
            elif et == "trend":
                st["trend"] += 1
            elif et == "time":
                st["time"] += 1
            elif et == "expired":
                st["expired"] += 1
            if r.get("hit_target"):
                st["target_hit"] += 1
            st["conf_sum"] += float(s.get("confidence") or 0)
            st["conf_count"] += 1
        else:
            st["pending"] += 1

    out = {}
    for strat, st in by_strategy.items():
        settled = st["settled"]
        out[strat] = {
            "total": st["total"], "settled": st["settled"], "pending": st["pending"], "unknown": st["unknown"],
            "stop": st["stop"], "trend": st["trend"], "time": st["time"],
            "expired": st["expired"], "target_hit": st["target_hit"],
            "stop_rate": round(st["stop"] / settled, 3) if settled else None,
            "non_stop_rate": round((settled - st["stop"]) / settled, 3) if settled else None,
            "target_hit_rate": round(st["target_hit"] / settled, 3) if settled else None,
            "avg_conf_settled": round(st["conf_sum"] / st["conf_count"], 1) if st["conf_count"] else None,
        }
    return {"strategies": out}


@router.get("/account")
def get_account(request: Request) -> dict:
    """账户余额（确认执行页拉取，用于计算默认预算 = 总余额×50%）。"""
    return request.app.state.executor.fetch_balance()


@router.post("/signals/{signal_id}/execute")
def execute_signal(signal_id: str, request: Request, payload: ExecuteRequest = Body(default=ExecuteRequest())) -> dict:
    """一键执行信号：按信号卡执行计划下单（dry_run 下仅模拟）并创建本地持仓。

    幂等：进程内互斥锁 + executed 标记，防并发重复下单（单进程 uvicorn 下有效）。
    budget_usdt: 确认页调整后的预算（缺省 = 总余额 × EXEC_DEFAULT_BUDGET_PCT = 50%）。
    """
    with _execute_lock:
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

        # ---------- 风控门禁：单日开仓上限 + 单日亏损熔断 ----------
        if db.count_positions_opened_today() >= config.BINANCE_DAILY_OPEN_LIMIT:
            raise HTTPException(status_code=429, detail=f"当日开仓已达上限 {config.BINANCE_DAILY_OPEN_LIMIT}")

        bal = executor.fetch_balance()
        if not bal.get("ok"):
            raise HTTPException(status_code=503, detail=f"余额查询失败: {bal.get('error')}")
        total = float(bal.get("total") or 0)
        pnl_today = db.sum_realized_pnl_today()
        if total > 0 and pnl_today < 0 and abs(pnl_today) / total >= config.BINANCE_DAILY_LOSS_LIMIT:
            raise HTTPException(status_code=429, detail="当日亏损已达熔断线，暂停新开仓")
        free = float(bal.get("free") or 0)
        # 默认预算 = 总余额 × EXEC_DEFAULT_BUDGET_PCT（50%）；用户确认页可传 budget_usdt 覆盖
        if payload.budget_usdt is not None and payload.budget_usdt > 0:
            budget = max(0.0, min(payload.budget_usdt, total))
        else:
            budget = total * config.EXEC_DEFAULT_BUDGET_PCT
        budget = min(budget, free)  # 不能超过可用余额
        # 70/30 拆分：市价 70% + 限价 30%（exec_plan.limit_price = 前阳 50% 回撤位）
        market_pct = int(exec_plan.get("market_pct") or config.EXEC_MARKET_PCT * 100) / 100
        # M5：按币种数量精度截断 + 最小下单量校验（高币价币种精度低，直接 round 会被币安拒单）
        market_amount = executor.amount_to_precision(symbol, round(budget * market_pct / price, 8))
        limit_amount = executor.amount_to_precision(symbol, round(budget * (1 - market_pct) / price, 8))
        amount = market_amount + limit_amount  # 总计划仓位（含限价腿）
        min_amt = executor.min_amount(symbol)
        if market_amount <= 0 or (min_amt > 0 and market_amount < min_amt):
            raise HTTPException(status_code=400,
                                detail=f"市价腿数量 {market_amount} 低于币种最小下单量 {min_amt}")
        # 币安 U 本位最小名义价值（开仓单非 reduceOnly 必须满足）
        notional = market_amount * price
        if notional < config.BINANCE_MIN_NOTIONAL:
            raise HTTPException(
                status_code=400,
                detail=f"下单名义价值 {notional:.2f} USDT 低于币安最小名义 "
                       f"{config.BINANCE_MIN_NOTIONAL} USDT（当前余额/预算不足，请充值或调高预算）",
            )

        # H3 原子占位：下单前先标记 executed（防并发/崩溃窗口"有单无记录"重复下单）
        if not db.mark_signal_executed(signal_id):
            raise HTTPException(status_code=409, detail="信号已被占用或执行")

        side = "buy" if direction == "long" else "sell"
        market = executor.create_order(symbol, side, market_amount, order_type="market")
        if not market.get("ok"):
            db.unmark_signal_executed(signal_id)  # 下单失败回滚占位，允许重试
            raise HTTPException(status_code=502, detail=f"下单失败: {market.get('error')}")

        # 限价腿（30%）：挂限价单（exec_plan.limit_price）；失败不阻塞市价腿
        limit_order = None
        limit_price = exec_plan.get("limit_price")
        if limit_amount > 0 and limit_price:
            limit_order = executor.create_order(symbol, side, limit_amount,
                                                order_type="limit", price=float(limit_price))

        pid = db.create_position(symbol, direction, price, amount,
                                 stop_price=exec_plan.get("stop_loss") or None, stop_stage=1,
                                 strategy=sig.get("strategy") or "short", signal_id=signal_id)
        # H7：给持仓挂交易所侧 STOP_MARKET 止损单（进程外保护；失败不阻塞，position_monitor 兜底）
        stop_price = exec_plan.get("stop_loss")
        if stop_price:
            stop_side = "sell" if direction == "long" else "buy"
            stop_order = executor.create_stop_loss_order(symbol, stop_side, amount, float(stop_price))
            if stop_order.get("ok"):
                db.update_position(pid, stop_order_id=stop_order.get("id"))
        sig["executed"] = True
        sig["executed_at"] = int(time.time() * 1000)
        sig["exec_side"] = side
        sig["exec_amount"] = amount
        db.save_signal(sig)

        return {
            "ok": True, "dry_run": market.get("dry_run"), "signal_id": signal_id,
            "position_id": pid, "side": side, "amount": amount,
            "order_id": market.get("id"), "price": price,
            "limit_order_id": limit_order.get("id") if limit_order else None,
            "budget_usdt": budget, "position_factor": pf, "mode": executor.mode_label,
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
def close_position(position_id: int, request: Request) -> dict:
    """手动平仓：真正向币安发 reduceOnly 平仓单（H4/H5），成功后写回已实现盈亏（M3）。"""
    pos = db.get_position(position_id)
    if pos is None or pos.get("status") != "open":
        raise HTTPException(status_code=404, detail="持仓不存在或已平仓")
    executor = request.app.state.executor
    if not executor.configured:
        raise HTTPException(status_code=503, detail="未配置币安 API Key/Secret")

    # 现价（平仓估算价，优先最新 15m 收盘）
    price = float(pos.get("entry_price") or 0)
    try:
        df = request.app.state.fetcher.fetch_ohlcv(pos["symbol"], config.TIMEFRAMES["entry"], limit=2)
        if df is not None and len(df):
            price = float(df["close"].iloc[-1])
    except Exception:  # noqa: BLE001 - 行情不可用时按入场价估算
        pass

    # H7：平仓前先撤交易所侧止损单（防平仓后止损单残留）
    stop_order_id = pos.get("stop_order_id")
    if stop_order_id:
        executor.cancel_order(pos["symbol"], stop_order_id)
    side = "sell" if pos["direction"] == "long" else "buy"
    qty = float(pos.get("qty") or 0)
    order = executor.create_order(pos["symbol"], side, qty, order_type="market", reduce_only=True)
    if not order.get("ok"):
        raise HTTPException(status_code=502, detail=f"平仓下单失败: {order.get('error')}")

    entry = float(pos.get("entry_price") or 0)
    pnl = (price - entry) * qty if pos["direction"] == "long" else (entry - price) * qty
    db.update_position(position_id, status="closed", realized_pnl=round(pnl, 8),
                       closed_at=datetime.now(timezone.utc).isoformat())
    return {"ok": True, "dry_run": order.get("dry_run"), "price": price,
            "realized_pnl": round(pnl, 8), "order_id": order.get("id")}


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
    if key not in _SETTING_ALLOWLIST:
        raise HTTPException(status_code=400, detail="该配置项不允许运行时修改")
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
    # H6：WS 鉴权（浏览器 WS 无法带 header，用 query ?token=xxx）
    token = getattr(config, "APP_AUTH_TOKEN", "") or ""
    if token and ws.query_params.get("token") != token:
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        while True:
            # 客户端消息（watchlist/持仓操作）M4 处理，先保持连接
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:  # noqa: BLE001
        manager.disconnect(ws)
