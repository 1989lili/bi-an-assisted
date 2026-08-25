"""信号引擎：10 层关卡流水线（产品文档 v4 §4）。

0️⃣ 市场环境 → 1️⃣ 方向门 → 2️⃣ 双周期扳机 → 3️⃣ 量能否决 → 4️⃣ 风控刹车
→ 5️⃣ K线形态 → 6️⃣ 宏观静默 → 7️⃣ 执行参数 → 8️⃣ 持仓管理(接口) → 9️⃣ 生命周期

平行：🔟 出场预警引擎（对持仓评估）。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .. import config
from ..indicators.engine import (
    compute_funding_tier,
    compute_indicator_snapshot,
    nearest_zone_distance,
    volatility_coef,
)
from .scorer import score_signal

# 15 分钟周期毫秒数（K 线收盘判定）
_BAR_MS = 15 * 60 * 1000
# K 线已运行超过 10 分钟（旱地拔葱例外条件）
_EXCEPTION_ELAPSED_MS = 10 * 60 * 1000


@dataclass
class SignalCard:
    """入场信号卡（TECH_DESIGN.md §4.3 结构）。"""

    symbol: str
    direction: str
    confidence: int
    levels: dict
    trigger_level: str
    funding: dict
    execution: dict
    reason: str
    created_at: int = field(default_factory=lambda: int(time.time() * 1000))
    expires_at: int = 0
    status: str = "pending_confirm"  # pending_confirm/confirmed/expired/stopped_out
    id: str = ""
    strategy: str = "short"           # short（短线扳机）/ ema_trend（策略一趋势跟踪）
    live_price: Optional[float] = None    # 高频监控：最新价（60s 更新）
    live_updated_at: Optional[int] = None  # 最新价更新时间（ms）

    def __post_init__(self) -> None:
        self.id = f"sig_{self.created_at}_{self.symbol.replace('/', '').replace(':', '')}_{self.direction}"
        if self.strategy == "ema_trend":
            # 策略一出场由 EMA50/吊灯/时间止损判定，不按短线有效期作废
            self.expires_at = self.created_at + 90 * 24 * 3600 * 1000
        else:
            self.expires_at = self.created_at + config.SIGNAL_TTL_BARS * _BAR_MS

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction,
            "confidence": self.confidence,
            "levels": self.levels,
            "trigger_level": self.trigger_level,
            "funding": self.funding,
            "execution": self.execution,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "strategy": self.strategy,
            "live_price": self.live_price,
            "live_updated_at": self.live_updated_at,
        }


class SignalEngine:
    """10 层关卡流水线。对单币评估，产出 SignalCard 或 None（含否决原因记录）。"""

    def __init__(self) -> None:
        self.rejections: dict[str, str] = {}  # symbol → 最近否决原因（调试用）

    # ==================== 主入口 ====================

    def evaluate(
        self,
        symbol: str,
        klines: dict,
        market_env: dict,
        funding_history: list[dict],
        oi_change: float | None,
        zones: list[tuple[float, float]],
    ) -> Optional[SignalCard]:
        """对单币执行完整流水线。klines: {'4h','1h','15m','5m': DataFrame}。"""
        snap = compute_indicator_snapshot(klines)
        s4h, s1h, s15m, s5m = snap.get("4h"), snap.get("1h"), snap.get("15m"), snap.get("5m")
        if not all([s4h, s1h, s15m, s5m]):
            self.rejections[symbol] = "数据不足（<60 根 K 线）"
            return None

        # ---------- 0️⃣ 市场环境 ----------
        env = market_env.get("env", "neutral")
        direction = self._direction_gate(s4h, s1h)
        if direction is None:
            self.rejections[symbol] = "方向门未开（趋势不一致或 ADX 不足）"
            return None
        if direction == "long" and env == "bear":
            self.rejections[symbol] = "市场环境拦截：大盘空头，不做多"
            return None
        if direction == "short" and env == "bull":
            self.rejections[symbol] = "市场环境拦截：大盘多头，不做空"

        # ---------- 2️⃣ 双周期扳机 ----------
        trigger_level = self._trigger(s15m, s5m, direction)
        if trigger_level is None:
            self.rejections[symbol] = "扳机未触发（需 5m MACD 柱同向确认）"
            return None
        if trigger_level == "C":
            self.rejections[symbol] = "仅 C 级扳机（RSI 穿越无量能），只观察"
            return None

        # ---------- 3️⃣ 量能否决 ----------
        if not self._volume_veto(s15m, oi_change):
            self.rejections[symbol] = "量能否决：量比不足且 OI 无增长"
            return None

        # ---------- 4️⃣ 风控刹车 ----------
        funding_tier = compute_funding_tier(funding_history)
        risk = self._risk_brake(s15m, s1h, direction, funding_tier, zones)
        if risk is None:
            self.rejections[symbol] = "风控刹车拦截（费率/清算距离/盈亏比）"
            return None

        # ---------- 5️⃣ K线形态 ----------
        candle_state = self._candle_check(s15m, s5m, direction)
        if candle_state is None:
            self.rejections[symbol] = "K线形态不满足（收盘破前低或影线过长）"
            return None

        # ---------- 6️⃣ 宏观静默 ----------
        if self._macro_silence():
            self.rejections[symbol] = "宏观静默期，暂停新开仓"
            return None

        # ---------- 7️⃣ 执行参数 ----------
        execution = self._execution_plan(s15m, direction, risk, funding_tier)

        # ---------- 打分 ----------
        confidence = score_signal(
            market_env, s4h, s1h, s15m, trigger_level, funding_tier, risk["liq_dist_atr"],
            volume_ratio=s15m.get("volume_ratio"), oi_change=oi_change,
        )
        if confidence < 50:
            self.rejections[symbol] = f"置信度不足（{confidence} 分）"
            return None

        levels = {
            "market_env": env,
            "direction_gate": True,
            "trigger": trigger_level,
            "volume_veto": True,
            "risk_brake": True,
            "candle_check": candle_state,
            "macro_silence": True,
        }
        reason = self._build_reason(direction, trigger_level, s15m, risk, funding_tier, candle_state)
        card = SignalCard(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            levels=levels,
            trigger_level=trigger_level,
            funding=funding_tier,
            execution=execution,
            reason=reason,
        )
        card.status = "confirmed" if candle_state in ("confirmed", "exception") else "pending_confirm"
        return card

    # ==================== 1️⃣ 方向门 ====================

    def _direction_gate(self, s4h: dict, s1h: dict) -> Optional[str]:
        """4h EMA55 + 1h EMA7/21 排列 + 4h MACD 零轴，三者一致才开门；ADX<25 关门。"""
        if s4h["adx"] < config.ADX_TREND_TH:
            return None
        long_ok = (
            s4h["above_ema55"] and s1h["ema7_above_21"] and s4h["macd_above_zero"]
        )
        short_ok = (
            not s4h["above_ema55"] and not s1h["ema7_above_21"] and not s4h["macd_above_zero"]
        )
        if long_ok:
            return "long"
        if short_ok:
            return "short"
        return None

    # ==================== 2️⃣ 双周期扳机 ====================

    def _trigger(self, s15m: dict, s5m: dict, direction: str) -> Optional[str]:
        """15m 主导 + 5m MACD 柱同向确认。A 回踩 / B 突破 / C 仅 RSI 穿越。"""
        # 5m 确认：MACD 柱同向，且同号连续根数 ≤ TRIGGER_MOMENTUM_BARS（N0.1 放宽：翻色后数根内仍有效）
        m5_streak = s5m.get("macd_hist_streak", 1)
        if direction == "long":
            m5_ok = s5m["macd_hist"] > 0 and m5_streak <= config.TRIGGER_MOMENTUM_BARS
        else:
            m5_ok = s5m["macd_hist"] < 0 and m5_streak <= config.TRIGGER_MOMENTUM_BARS
        if not m5_ok:
            return None

        atr15 = s15m["atr"]
        if direction == "long":
            # A 级：回踩 15m EMA21 附近（±0.5×ATR）不破 + 缩量 + 启动
            near_ema = abs(s15m["last_low"] - s15m["ema21"]) <= 0.5 * atr15
            shrink = s15m["volume_ratio"] is not None and s15m["volume_ratio"] < config.VOL_RATIO_LOW
            bounce = s15m["close"] > s15m["ema21"] and s15m["rsi"] > s15m["rsi_prev"]
            if near_ema and shrink and bounce:
                return "A"
            # B 级：放量突破前高
            breakout = s15m["close"] > s15m["recent_high"]
            hot = s15m["volume_ratio"] is not None and s15m["volume_ratio"] > config.VOL_RATIO_HOT
            if breakout and hot:
                return "B"
        else:
            near_ema = abs(s15m["last_high"] - s15m["ema21"]) <= 0.5 * atr15
            shrink = s15m["volume_ratio"] is not None and s15m["volume_ratio"] < config.VOL_RATIO_LOW
            bounce = s15m["close"] < s15m["ema21"] and s15m["rsi"] < s15m["rsi_prev"]
            if near_ema and shrink and bounce:
                return "A"
            breakdown = s15m["close"] < s15m["recent_low"]
            hot = s15m["volume_ratio"] is not None and s15m["volume_ratio"] > config.VOL_RATIO_HOT
            if breakdown and hot:
                return "B"
        # C 级：RSI 穿越 50（主扳机）
        if direction == "long" and s15m["rsi_cross_up_50"]:
            return "C"
        if direction == "short" and not s15m["rsi_cross_up_50"] and s15m["rsi"] < 50 and s15m["rsi_prev"] >= 50:
            return "C"
        return None

    # ==================== 3️⃣ 量能否决 ====================

    def _volume_veto(self, s15m: dict, oi_change: float | None) -> bool:
        """量比 <1.2 且 OI 变化率 <+1% → 一票否决。"""
        vr = s15m["volume_ratio"]
        if vr is None:
            return False
        if vr < config.VOL_RATIO_VETO and (oi_change is None or oi_change < config.OI_GROWTH_VETO):
            return False
        return True

    # ==================== 4️⃣ 风控刹车 ====================

    def _risk_brake(
        self,
        s15m: dict,
        s1h: dict,
        direction: str,
        funding_tier: dict,
        zones: list[tuple[float, float]],
    ) -> Optional[dict]:
        """费率档位 + 清算距离（带宽自适应）+ 盈亏比 ≥2。"""
        # 费率危险档：拦截做多（多头拥挤）；做空不受影响
        if funding_tier.get("tier") == "danger" and direction == "long":
            return None

        coef = volatility_coef(s15m["bw"], s15m["bw_median"])
        price = s15m["close"]
        atr15 = s15m["atr"]
        stop_dist = coef * atr15

        # 清算距离检查
        liq_dist = nearest_zone_distance(price, zones, direction)
        liq_dist_atr = (liq_dist / atr15) if liq_dist is not None else None
        if liq_dist is not None and liq_dist < stop_dist:
            return None  # 现价距清算密集区太近，可能接针

        # 盈亏比：目标 = 1h 最近 swing 高点/低点；无参考则按 2.5 倍止损距离估算
        if direction == "long":
            swings = s1h.get("swing_highs") or []
            target = swings[-1] if swings else price + 2.5 * stop_dist
            if target <= price:
                target = price + 2.5 * stop_dist
            rr = (target - price) / stop_dist
            if rr < config.MIN_RISK_REWARD:
                return None  # 上方空间不足
            stop_loss = price - stop_dist
        else:
            swings = s1h.get("swing_lows") or []
            target = swings[-1] if swings else price - 2.5 * stop_dist
            if target >= price:
                target = price - 2.5 * stop_dist
            rr = (price - target) / stop_dist
            if rr < config.MIN_RISK_REWARD:
                return None
            stop_loss = price + stop_dist

        return {
            "coef": coef,
            "stop_dist": stop_dist,
            "stop_loss": stop_loss,
            "target": target,
            "risk_reward": round(rr, 2),
            "liq_dist": liq_dist,
            "liq_dist_atr": liq_dist_atr,
        }

    # ==================== 5️⃣ K线形态硬刹车 ====================

    def _candle_check(self, s15m: dict, s5m: dict, direction: str) -> Optional[str]:
        """常规：15m 收盘后检查（收盘不破前低 + 实体>影线）。
        例外（旱地拔葱）：当前 K 线运行 >10 分钟且突破前高 ≥1.5×ATR → 直接放行。
        返回 'confirmed' / 'exception' / 'pending'；形态不满足返回 None。
        """
        now_ms = int(time.time() * 1000)
        bar_elapsed = now_ms - s15m["last_ts"]
        closed = bar_elapsed >= _BAR_MS  # 最后一根 15m K 线已收盘

        atr15 = s15m["atr"]
        if direction == "long":
            exceeded = s15m["close"] > s15m["recent_high"] + 1.5 * atr15
            body_ok = s15m["body"] >= s15m["shadow"]
            prev_low_ok = s15m["close"] > s15m["prev_low"]
        else:
            exceeded = s15m["close"] < s15m["recent_low"] - 1.5 * atr15
            body_ok = s15m["body"] >= s15m["shadow"]
            prev_low_ok = s15m["close"] < s15m["prev_high"]

        if not closed:
            # 旱地拔葱例外：临近收盘（>10 分钟）+ 远超确认条件
            if bar_elapsed > _EXCEPTION_ELAPSED_MS and exceeded:
                return "exception"
            return "pending"  # 等待 K 线收盘确认
        if body_ok and prev_low_ok:
            return "confirmed"
        return None

    # ==================== 6️⃣ 宏观静默期 ====================

    def _macro_silence(self) -> bool:
        """CPI/非农/FOMC 等事件前后 15 分钟暂停开仓（内置表 + 手动维护）。"""
        from ..store import db

        now = time.time()
        rows = db.get_macro_events()  # [{title, event_time(iso)}]
        window = config.MACRO_SILENCE_MINUTES * 60
        for row in rows:
            try:
                event_ts = _parse_iso_ms(row["event_time"])
            except (ValueError, TypeError):
                continue
            if abs(now - event_ts) <= window:
                return True
        return False

    # ==================== 7️⃣ 执行参数 ====================

    def _execution_plan(self, s15m: dict, direction: str, risk: dict, funding_tier: dict) -> dict:
        """市价 70% + 前一根 15m K 线 50% 回撤位限价 30%（45 分钟失效）。"""
        price = s15m["close"]
        prev_mid = (s15m["prev_high"] + s15m["prev_low"]) / 2
        if direction == "long":
            limit_price = max(prev_mid, price - risk["stop_dist"] * 0.3)  # 不挂到止损下方
        else:
            limit_price = min(prev_mid, price + risk["stop_dist"] * 0.3)
        return {
            "market_price": round(price, 8),
            "market_pct": int(config.EXEC_MARKET_PCT * 100),
            "limit_pct": int(config.EXEC_LIMIT_PCT * 100),
            "limit_price": round(limit_price, 8),
            "stop_loss": round(risk["stop_loss"], 8),
            "target": round(risk["target"], 8),
            "risk_reward": risk["risk_reward"],
            "stop_dist": round(risk["stop_dist"], 8),
            "position_factor": round(
                funding_tier.get("position_factor", 1.0) * self._vol_factor(s15m["atr"], s15m["close"]), 3
            ),
            "limit_ttl_bars": config.EXEC_LIMIT_TTL_BARS,
        }

    def _vol_factor(self, atr: float, price: float) -> float:
        """波动率目标仓位系数（N0.5）：实际波动高于目标 → 降仓；低于目标不放大仓位。

        factor = 目标 ATR% / 实际 ATR%，截断到 [VOL_FACTOR_MIN, VOL_FACTOR_MAX]。
        """
        if atr <= 0 or price <= 0:
            return 1.0
        vol_pct = atr / price
        if vol_pct <= 0:
            return 1.0
        factor = config.VOL_TARGET_ATR_PCT / vol_pct
        return max(config.VOL_FACTOR_MIN, min(config.VOL_FACTOR_MAX, factor))

    # ==================== 理由文本 ====================

    def _build_reason(self, direction: str, trigger_level: str, s15m: dict, risk: dict, funding_tier: dict, candle_state: str) -> str:
        d = "做多" if direction == "long" else "做空"
        parts = [
            f"{trigger_level}级扳机（{'回踩企稳' if trigger_level == 'A' else '放量突破'}）",
            f"量比 {s15m['volume_ratio']:.2f}",
        ]
        if funding_tier.get("tier") == "stable_high":
            parts.append("费率高位稳定，仓位 ×0.7")
        if candle_state == "exception":
            parts.append("旱地拔葱例外：直接市价")
        # 止损/目标/盈亏比由 execution 结构化字段展示，reason 不再重复
        return f"{d}信号 - " + "，".join(parts)


def _parse_iso_ms(iso: str) -> float:
    """ISO 时间字符串 → 秒时间戳。"""
    from datetime import datetime

    dt = datetime.fromisoformat(iso)
    return dt.timestamp()


# ==================== 🔟 出场预警引擎 ====================


def evaluate_exit(snap: dict, position: dict) -> list[dict]:
    """对持仓评估出场预警（产品文档 §4-🔟）。snap: 多周期指标快照。

    position: {direction, entry_price, stop_stage, stop_price}
    返回预警列表 [{type, level, message, action}]。
    """
    s15m = snap.get("15m")
    s1h = snap.get("1h")
    if not s15m or not s1h:
        return []
    alerts = []
    direction = position["direction"]
    entry = position["entry_price"]
    price = s15m["close"]

    # 技术反转：同级别死叉 / 1h 跌破 EMA21
    if direction == "long":
        ema_dead = not s15m["ema7_above_21"]
        trend_break = s1h["close"] < s1h["ema21"]
        rsi_div = s15m["rsi"] > 70  # 过热（简化版背离检测）
        if ema_dead or trend_break:
            alerts.append({"type": "technical_reversal", "level": "high",
                           "message": "15m EMA7 下穿 EMA21 或 1h 跌破 EMA21", "action": "平仓或减仓50%"})
        elif rsi_div:
            alerts.append({"type": "rsi_overheat", "level": "medium",
                           "message": "15m RSI 超买（过热）", "action": "关注回调风险"})
    else:
        ema_gold = s15m["ema7_above_21"]
        trend_break = s1h["close"] > s1h["ema21"]
        if ema_gold or trend_break:
            alerts.append({"type": "technical_reversal", "level": "high",
                           "message": "15m EMA7 上穿 EMA21 或 1h 站上 EMA21", "action": "平仓或减仓50%"})

    # 止损逼近
    stop = position.get("stop_price")
    if stop:
        atr15 = s15m["atr"]
        dist = abs(price - stop)
        if dist < 0.5 * atr15:
            alerts.append({"type": "stop_approaching", "level": "medium",
                           "message": f"价格距当前止损 {dist / atr15:.2f}×ATR", "action": "准备执行止损"})
    return alerts
