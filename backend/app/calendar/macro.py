"""宏观静默期内置事件表（TECH_DESIGN §4.6 / §7-M5）。

内置常规宏观事件（美国 CPI / 非农 / FOMC 利率决议 / 议息发布会）按规律性规则**估算**
近期事件点，用于宏观静默期判定。

这些是**规则估算**，官方实际公布日期可能略有偏移，建议以实际财经日历为准——用户可在
「设置」页修正或删除；用户手动新增的事件为 `manual`，内置写入的为 `builtin`，两者均
参与静默判定。

静默判定（signal/engine.py `_macro_silence`）：`now ∈ [事件时间 − 前后窗口, 事件时间 + 前后窗口]`。
过时事件因与当前时刻差值过大，不会误触发。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..store import db

logger = logging.getLogger(__name__)

# ---- 各事件生成规则（估算点，时间按 UTC） ----
FOMC_MONTHS = (3, 6, 9, 12)   # 议息会议每季度一次
CPI_DAY = 13                  # CPI 月中估算
NFP_WEEKDAY = 4               # 非农：每月第一个周五（weekday=4=周五）
EVENT_HOUR = 20               # 事件估算时刻（UTC 20:30）


def _next_weekday(d: datetime, weekday: int) -> datetime:
    """返回 d 当天或之后第一个 weekday 对应的日期（保留时分秒）。"""
    days = (weekday - d.weekday()) % 7
    return d + timedelta(days=days)


def estimate_builtin_events(now: datetime | None = None, months: int = 2) -> list[dict]:
    """估算未来 months 个月内的常规宏观事件。

    返回 `[{title, event_time(ISO UTC)}]`；事件时刻统一取当日 20:30 UTC 作为估算点，
    仅保留未过去的。
    """
    now = now or datetime.now(timezone.utc)
    events: list[dict] = []
    y, mo = now.year, now.month
    for _ in range(months):
        first = datetime(y, mo, 1, EVENT_HOUR, 30, tzinfo=timezone.utc)
        # 非农：当月第一个周五
        nfp = _next_weekday(first, NFP_WEEKDAY)
        events.append({"title": "非农就业数据", "event_time": nfp.isoformat()})
        # CPI：当月 13 日
        events.append(
            {
                "title": "CPI 通胀数据",
                "event_time": datetime(y, mo, CPI_DAY, EVENT_HOUR, 30, tzinfo=timezone.utc).isoformat(),
            }
        )
        # FOMC 利率决议 + 主席发布会（每季度）
        if mo in FOMC_MONTHS:
            second_wed = _next_weekday(first, 2) + timedelta(days=7)
            events.append({"title": "FOMC 利率决议", "event_time": second_wed.isoformat()})
            events.append(
                {"title": "FOMC 主席发布会", "event_time": (second_wed + timedelta(minutes=30)).isoformat()}
            )
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


def seed_builtin_events() -> None:
    """启动时若宏观事件表为空，则写入内置估算事件（`source='builtin'`）。

    已有事件（用户手动维护）时跳过，避免重复灌入；内置事件同样可被用户在设置页删除/修正。
    """
    if db.get_macro_events():
        return
    builtins = estimate_builtin_events()
    for e in builtins:
        db.add_macro_event(e["title"], e["event_time"], source="builtin")
    logger.info("宏观静默期内置事件已写入: %s 条", len(builtins))
