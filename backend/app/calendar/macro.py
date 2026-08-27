"""宏观静默期内置事件表（TECH_DESIGN §4.6 / §7-M5）。

内置常规美国高影响力宏观事件按规律性规则**估算**近期事件点，用于宏观静默期判定：

- 新开仓：事件前后 `MACRO_SILENCE_MINUTES` 分钟暂停（signal/engine.py `_macro_silence`）。
- 已持仓：窗口内由 PositionMonitor 收紧止损 / 可选减仓（防插针）。

覆盖事件（均为美国官方数据 / 发布会，8:30 ET 或 10:00 ET 或 FOMC 14:00 ET）：
非农 NFP、CPI、PPI、零售销售、耐用品订单、密歇根消费者信心、ISM 制造业/非制造业 PMI、
初请失业金（每周四）、季度 GDP、FOMC 利率决议、FOMC 鲍威尔主席发布会。

这些是**规则估算**，官方实际公布日期可能略有偏移（如 CPI 逢 13 日落在周末会顺延），
建议以实际财经日历为准——用户可在「设置」页修正或删除；用户手动新增的事件为
`manual`，内置写入的为 `builtin`，两者均参与静默判定。

静默判定（公共函数 `in_silence_window`）：
`now ∈ [事件时间 − 前后窗口, 事件时间 + 前后窗口]`，过时事件不会误触发。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from ..store import db

logger = logging.getLogger(__name__)

FOMC_MONTHS = (3, 6, 9, 12)        # FOMC 议息会议每季度一次
GDP_MONTHS = (1, 4, 7, 10)         # GDP 季报发布月（季度结束后次月，含初次/修正值）

# 月度常规数据：(标题, 估算日, 美东时刻)
_MONTHLY_EVENTS = (
    ("ISM 制造业 PMI", 1, (10, 0)),
    ("ISM 非制造业 PMI", 3, (10, 0)),
    ("CPI 通胀数据", 13, (8, 30)),
    ("PPI 生产者物价指数", 14, (8, 30)),
    ("零售销售数据", 16, (8, 30)),
    ("密歇根大学消费者信心", 18, (10, 0)),
    ("耐用品订单", 26, (8, 30)),
    ("美国 GDP（季度）", 28, (8, 30)),
)


# ---------------- 美东时间 → UTC（含夏令时规则） ----------------

def _is_dst(d: date) -> bool:
    """美国夏令时：3 月第二个周日 02:00 至 11 月第一个周日 02:00（UTC-4），其余 UTC-5。"""
    mar = date(d.year, 3, 1)
    second_sun_mar = mar + timedelta(days=(6 - mar.weekday()) % 7 + 7)
    nov = date(d.year, 11, 1)
    first_sun_nov = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return second_sun_mar <= d < first_sun_nov


def _et_to_utc(y: int, mo: int, day: int, hour: int, minute: int) -> datetime:
    """美东 (y,mo,day) 时刻 → UTC aware datetime（UTC = ET + 4/5 小时）。"""
    offset_h = 4 if _is_dst(date(y, mo, day)) else 5
    local = datetime(y, mo, day, hour, minute) + timedelta(hours=offset_h)
    return local.replace(tzinfo=timezone.utc)


def _next_weekday(d: date, weekday: int) -> date:
    """返回 d 当天或之后第一个 weekday 对应的日期（weekday: 0=周一 … 4=周五 …）。"""
    days = (weekday - d.weekday()) % 7
    return d + timedelta(days=days)


# ---------------- 内置事件生成 ----------------

def estimate_builtin_events(now: datetime | None = None, months: int = 2) -> list[dict]:
    """估算未来 months 个月内的常规宏观事件。

    返回 `[{title, event_time(ISO UTC)}]`；仅保留未过去的。
    """
    now = now or datetime.now(timezone.utc)
    events: list[dict] = []
    y, mo = now.year, now.month
    for _ in range(months):
        first = date(y, mo, 1)

        # 初请失业金：当月每周四 8:30 ET（高波动周度数据，影响力中等但频率高）
        thu = _next_weekday(first, 3)
        while thu.month == mo:
            events.append({
                "title": "初请失业金人数",
                "event_time": _et_to_utc(y, mo, thu.day, 8, 30).isoformat(),
            })
            thu += timedelta(days=7)

        # 月度常规数据
        for title, day, (hh, mm) in _MONTHLY_EVENTS:
            if title.startswith("美国 GDP") and mo not in GDP_MONTHS:
                continue
            events.append({
                "title": title,
                "event_time": _et_to_utc(y, mo, day, hh, mm).isoformat(),
            })

        # 非农：当月第一个周五 8:30 ET
        nfp = _next_weekday(first, 4)
        events.append({
            "title": "非农就业数据",
            "event_time": _et_to_utc(y, mo, nfp.day, 8, 30).isoformat(),
        })

        # FOMC 利率决议 + 鲍威尔主席发布会（每季度第二个周三，14:00 / 14:30 ET）
        if mo in FOMC_MONTHS:
            second_wed = _next_weekday(first, 2) + timedelta(days=7)
            events.append({
                "title": "FOMC 利率决议",
                "event_time": _et_to_utc(y, mo, second_wed.day, 14, 0).isoformat(),
            })
            events.append({
                "title": "FOMC 鲍威尔主席发布会",
                "event_time": _et_to_utc(y, mo, second_wed.day, 14, 30).isoformat(),
            })

        # 推进月份
        mo += 1
        if mo > 12:
            mo = 1
            y += 1

    now_ts = now.timestamp()
    return [
        e for e in events
        if datetime.fromisoformat(e["event_time"]).timestamp() > now_ts
    ]


def reseed_builtin_events(now: datetime | None = None) -> None:
    """清空旧内置事件并按最新规则重播种（每次启动同步，规则更新后立即生效）。

    只删除 `source='builtin'` 的条目；用户手动维护的 `manual` 事件不受影响。
    用户若在设置页删除过某内置事件，重启后会按规则重新加入（可再次删除）。
    """
    db.delete_macro_events_by_source("builtin")
    for e in estimate_builtin_events(now):
        db.add_macro_event(e["title"], e["event_time"], source="builtin")
    logger.info("宏观静默期内置事件已按新规则重播种: %s 条", len(estimate_builtin_events(now)))


# ---------------- 静默判定（公共） ----------------

def in_silence_window(now: datetime | None = None) -> bool:
    """当前时刻是否处于任一宏观事件的前后静默窗口内。"""
    from .. import config

    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    window = config.MACRO_SILENCE_MINUTES * 60
    for row in db.get_macro_events():
        try:
            event_ts = datetime.fromisoformat(row["event_time"]).timestamp()
        except (ValueError, TypeError):
            continue
        if abs(now_ts - event_ts) <= window:
            return True
    return False


def next_macro_event(now: datetime | None = None) -> dict | None:
    """返回下一个（含正在窗口内）宏观事件 `{title, event_time, within}`，无则 None。"""
    from .. import config

    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    window = config.MACRO_SILENCE_MINUTES * 60
    upcoming = []
    for row in db.get_macro_events():
        try:
            event_ts = datetime.fromisoformat(row["event_time"]).timestamp()
        except (ValueError, TypeError):
            continue
        if event_ts + window < now_ts:
            continue  # 已过（含窗口）
        upcoming.append((event_ts, row["title"]))
    if not upcoming:
        return None
    ts, title = min(upcoming)
    return {
        "title": title,
        "event_time": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "within": abs(now_ts - ts) <= window,
    }
