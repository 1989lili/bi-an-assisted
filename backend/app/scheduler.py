"""任务调度：粗筛（1 分钟）+ 精扫（5 分钟）+ 信号监控（1 分钟）循环。"""
import logging
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from . import config

logger = logging.getLogger(__name__)


def build_scheduler(
    coarse_scanner,
    deep_scanner,
    on_scan_complete: Optional[Callable[[list], None]] = None,
    signal_monitor=None,
    on_monitor_update: Optional[Callable[[list], None]] = None,
) -> BackgroundScheduler:
    """创建后台调度器，注册粗筛、精扫与信号监控任务。

    on_scan_complete: 每轮精扫完成后回调（接收信号列表），用于 WS 广播。
    signal_monitor: 活跃信号高频监控器；on_monitor_update 为变更回调（signal:update 广播）。
    """
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    def deep_job() -> None:
        signals = deep_scanner.scan()
        if on_scan_complete is not None:
            on_scan_complete(signals)

    scheduler.add_job(
        coarse_scanner.scan,
        "interval",
        seconds=config.COARSE_INTERVAL_SEC,
        id="coarse_scan",
        max_instances=1,
        coalesce=True,  # 上一轮未结束时跳过本轮，避免堆积
    )
    scheduler.add_job(
        deep_job,
        "interval",
        seconds=config.DEEP_INTERVAL_SEC,
        id="deep_scan",
        max_instances=1,
        coalesce=True,
    )
    jobs = ["粗筛 %ss" % config.COARSE_INTERVAL_SEC, "精扫 %ss" % config.DEEP_INTERVAL_SEC]
    if signal_monitor is not None and on_monitor_update is not None:
        signal_monitor.set_on_update(on_monitor_update)
        scheduler.add_job(
            signal_monitor.check,
            "interval",
            seconds=config.SIGNAL_MONITOR_INTERVAL_SEC,
            id="signal_monitor",
            max_instances=1,
            coalesce=True,
        )
        jobs.append("信号监控 %ss" % config.SIGNAL_MONITOR_INTERVAL_SEC)
    logger.info("调度器已注册: %s", " / ".join(jobs))
    return scheduler
