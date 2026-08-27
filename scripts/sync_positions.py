# -*- coding: utf-8 -*-
"""同步 DB 与实际：币安已无持仓 → 关闭 DB open 持仓 + 撤残留止损单。"""
from datetime import datetime, timezone

import app.store.db as db
from app.executor.binance import BinanceExecutor

ex = BinanceExecutor()

# 1) 撤币安残留止损单（若还在挂单簿）
for pos in db.get_positions("open"):
    sid = pos.get("stop_order_id")
    if sid:
        r = ex.cancel_order(pos["symbol"], sid)
        print("撤止损单", pos["symbol"], sid, "->", r.get("ok"), r.get("error", ""))

# 2) 校验币安持仓：无实际持仓的 DB open 记录直接关闭
closed_any = False
for pos in db.get_positions("open"):
    try:
        ex_pos = ex.fetch_position(pos["symbol"])
    except Exception as e:
        print("查询失败(跳过):", pos["symbol"], str(e)[:80])
        continue
    if ex_pos is None:
        db.update_position(pos["id"], status="closed", realized_pnl=0.0,
                           closed_at=datetime.now(timezone.utc).isoformat())
        print("关闭幽灵持仓:", pos["id"], pos["symbol"], pos["direction"])
        closed_any = True
    else:
        print("币安仍有持仓:", pos["symbol"], ex_pos["side"], ex_pos["contracts"], "(DB 保留)")

print("剩余 open 持仓:", [(p["id"], p["symbol"]) for p in db.get_positions("open")])
