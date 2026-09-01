"""标的启动感知策略（用户定制的第 3 套策略，框架版）。

五大启动信号指标（量化公式由用户陆续提供，当前为**框架骨架**，公式占位）：

  1. 成交量爆发   volume_burst          成交量异常放大
  2. 波动率扩张   volatility_expansion  波动率（ATR/布林带宽）扩张
  3. 均线         ma                    均线多头/空头排列启动
  4. 资金面质变   funding_change        资金费率 / OI 质变
  5. 多周期共振   multi_tf              多周期同向共振

组合规则（默认，可调整）：**全部指标通过**才触发信号。
- 与套1/策略一**独立并行**，不参与打分（confidence=0，前端橙色卡区分）。
- 指标明细写入信号卡 `levels_detail`（前端展示 5 项状态与判定值）。
- 单币每轮评估一次，去重/冷却沿用 scan/deep.py `_should_emit`（strategy='launch_sense'）。
"""
from __future__ import annotations

import time
from typing import Optional

import pandas as pd

from .. import config

# 触发所需的最少指标数（默认 5=全部；可调）
LAUNCH_SENSE_MIN_INDICATORS = 5


def _pass(value, note: str, ok: bool = True) -> dict:
    """指标结果：{pass, value, note}。"""
    return {"pass": ok, "value": value, "note": note}


# ==================== 指标骨架（公式待用户提供） ====================

def _volume_burst(klines: dict, direction: str) -> dict:
    """① 成交量爆发：骨架。公式待定义（如 当前15m量 > N×20均量 且 OI 同步放大）。"""
    return {"pass": False, "value": None, "note": "公式待定义（成交量爆发）"}


def _volatility_expansion(klines: dict, direction: str) -> dict:
    """② 波动率扩张：骨架。公式待定义（如 布林带宽/ATR 相对中位数扩张 > N 倍）。"""
    return {"pass": False, "value": None, "note": "公式待定义（波动率扩张）"}


def _ma(klines: dict, direction: str) -> dict:
    """③ 均线：骨架。公式待定义（如 15m/1h/4h 均线多头排列 且 价格站上短均线）。"""
    return {"pass": False, "value": None, "note": "公式待定义（均线）"}


def _funding_change(klines: dict, direction: str, funding_history: list[dict]) -> dict:
    """④ 资金面质变：骨架。公式待定义（如 费率由负转正/逼近极端 + OI 大幅变化）。"""
    return {"pass": False, "value": None, "note": "公式待定义（资金面质变）"}


def _multi_tf(klines: dict, direction: str) -> dict:
    """⑤ 多周期共振：骨架。公式待定义（如 15m/1h/4h 收盘方向一致 + MACD 同向）。"""
    return {"pass": False, "value": None, "note": "公式待定义（多周期共振）"}


# ==================== 组合评估 ====================

def evaluate(klines: dict, funding_history: list[dict] | None = None) -> Optional[dict]:
    """评估单币是否触发"标的启动"信号。

    klines: {'4h','1h','15m','5m': DataFrame}（与套1/策略一同源，缺周期则跳过）。
    返回 None 或 {
        "direction": "long"|"short",
        "indicators": {指标key: {pass,value,note}},
        "reason": str,
    }
    """
    for tf in ("15m", "1h", "4h"):
        df = klines.get(tf)
        if df is None or len(df) < 60:
            return None  # 数据不足（快照需 ≥60 根）

    # 方向：骨架暂定多头方向（公式待定义后按指标方向/投票决定）
    direction = "long"

    indicators = {
        "volume_burst": _volume_burst(klines, direction),
        "volatility_expansion": _volatility_expansion(klines, direction),
        "ma": _ma(klines, direction),
        "funding_change": _funding_change(klines, direction, funding_history or []),
        "multi_tf": _multi_tf(klines, direction),
    }
    passed = [k for k, v in indicators.items() if v.get("pass")]

    # 框架阶段：公式未定义（全 False）不触发；公式就位后按达标数判定
    if len(passed) < LAUNCH_SENSE_MIN_INDICATORS:
        return None

    return {
        "direction": direction,
        "indicators": indicators,
        "reason": "标的启动感知（" + "/".join(passed) + " 达标）",
    }
