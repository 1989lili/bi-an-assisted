"""M3 后端服务入口：FastAPI + WebSocket + 调度器 + 静态托管（TECH_DESIGN.md §2）。

运行：cd backend; D:\\miniconda3\\envs\\py12\\python.exe -m app.main
访问：http://localhost:8000（手机同局域网访问 http://<本机IP>:8000）
"""
import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config

from .api.routes import router
from .calendar.macro import reseed_builtin_events
from .data.fetcher import BinanceFetcher
from .executor.binance import BinanceExecutor
from .notify.ws import manager
from .scan.coarse import CoarseScanner
from .scan.deep import DeepScanner
from .scheduler import build_scheduler
from .signal.monitor import SignalMonitor
from .store import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：初始化存储 → 构建扫描器 → 启动调度器 → 首轮精扫立即执行。"""
    db.init_db()
    reseed_builtin_events()
    fetcher = BinanceFetcher()
    coarse = CoarseScanner(fetcher)
    deep = DeepScanner(fetcher, coarse)
    executor = BinanceExecutor()
    monitor = SignalMonitor(fetcher, executor)
    from .position.monitor import PositionMonitor

    position_monitor = PositionMonitor(fetcher, executor)

    def on_scan_complete(signals: list) -> None:
        """每轮精扫完成：广播信号与扫描报告 + 同步启动感知候选池。"""
        if signals:
            manager.broadcast(
                "signal:new", {"signals": [s.to_dict() for s in signals]}
            )
        # 启动感知：候选池同步（1h 流监听 L1+L2，在线维护 L3 观察集）
        launch_watch.update_watchlist(deep.last_pool)
        manager.broadcast(
            "scan:report",
            {
                "ts": deep.last_scan_ts,
                "candidate_count": len(deep.last_pool),
                "signal_count": len(signals),
                "market_env": deep.last_market_env,
                "candidates": list(deep.last_pool),
                "rejections": dict(deep.engine.rejections),
                "launch_pool_size": len(launch_watch.l1l2_pool),
                "launch_pool": sorted(launch_watch.l1l2_pool),
            },
        )

    def on_monitor_update(changed: list) -> None:
        """信号高频监控变更：实时价/状态更新广播（signal:update）。"""
        if changed:
            manager.broadcast("signal:update", {"signals": changed})

    def on_position_change(changed: list) -> None:
        """持仓风控变更：平仓广播（position:update）。"""
        if changed:
            manager.broadcast("position:update", {"positions": changed})

    def on_launch_signal(card) -> None:
        """启动感知实时信号 → 广播。"""
        manager.broadcast("signal:new", {"signals": [card.to_dict()]})

    # 启动感知 5m 监听器（L1+L2 小池实时评估 L3）
    from .scan.launch_watch import LaunchSenseWatcher

    launch_watch = LaunchSenseWatcher(fetcher, on_signal=on_launch_signal)
    launch_watch.start()

    scheduler = build_scheduler(
        coarse, deep, on_scan_complete=on_scan_complete,
        signal_monitor=monitor, on_monitor_update=on_monitor_update,
        position_monitor=position_monitor, on_position_change=on_position_change,
    )

    # 共享实例：API 路由通过 request.app.state 访问
    app.state.fetcher = fetcher
    app.state.coarse = coarse
    app.state.deep = deep
    app.state.monitor = monitor
    app.state.executor = executor
    app.state.position_monitor = position_monitor
    app.state.scheduler = scheduler
    app.state.launch_watch = launch_watch

    # WS 广播绑定主事件循环（调度线程经 run_coroutine_threadsafe 提交）
    manager.bind_loop(asyncio.get_running_loop())

    scheduler.start()
    # 首轮精扫后台执行（约 80s，不阻塞启动）
    threading.Thread(target=deep.scan, name="first-deep-scan", daemon=True).start()
    logger.info("服务启动完成: http://localhost:8000（调度器运行中）")
    yield
    scheduler.shutdown(wait=False)
    logger.info("服务已停止")


app = FastAPI(title="币安 U 本位合约辅助决策工具", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """H6 HTTP 鉴权：APP_AUTH_TOKEN 非空时，/api 请求需 Bearer header 或 ?token=。"""
    token = getattr(config, "APP_AUTH_TOKEN", "") or ""
    if token and request.url.path.startswith("/api"):
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {token}" or request.query_params.get("token") == token:
            pass
        else:
            return JSONResponse({"detail": "未授权访问"}, status_code=401)
    return await call_next(request)


app.include_router(router)

# 前端静态托管（M4 构建产物；目录不存在时 API 仍可用）
_frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _frontend_dist.exists():
    from fastapi.responses import FileResponse, JSONResponse

    # 静态资源（hash 文件名）直接托管
    app.mount("/assets", StaticFiles(directory=_frontend_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        """SPA 兜底：未匹配的 /api 路径返回 404 JSON，其余返回 index.html。"""
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "接口不存在"}, status_code=404)
        return FileResponse(_frontend_dist / "index.html", media_type="text/html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
