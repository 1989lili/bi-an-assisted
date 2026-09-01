# -*- coding: utf-8 -*-
"""列出当前信号及其满足条件（关卡判定值 + 评分明细）。"""
import json
import sqlite3

conn = sqlite3.connect(r"D:\git\bi-an-assisted\data\app.db")
conn.row_factory = sqlite3.Row

print("=== 持仓 ===")
for r in conn.execute(
    "SELECT id, symbol, direction, qty, stop_price, stop_order_id, leverage, status "
    "FROM positions WHERE status = 'open'"
):
    print(dict(r))

print("\n=== 信号 ===")
rows = conn.execute("SELECT id, card_json, created_at FROM signals ORDER BY created_at DESC").fetchall()
if not rows:
    print("（无信号记录）")
for r in rows:
    c = json.loads(r["card_json"])
    print("\n-", c.get("symbol"), c.get("direction"), "| 级别", c.get("trigger_level"),
          "| 置信度", c.get("confidence"), "| 状态", c.get("status"),
          "| 已执行", bool(c.get("executed")))
    ld = c.get("levels_detail") or {}
    if ld:
        print("  关卡判定值:")
        for cat, kv in ld.items():
            print(f"    {cat}: " + " | ".join(f"{k}={v}" for k, v in kv.items()))
    sd = c.get("score_detail") or []
    if sd:
        print("  评分明细:")
        for it in sd:
            print(f"    {it['cat']}·{it['name']}: {it['score']}/{it['max']} ({it['note']})")
