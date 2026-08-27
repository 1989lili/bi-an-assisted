# -*- coding: utf-8 -*-
"""Verify positions and remaining signals after C-level cleanup (dev helper)."""
import json
import sqlite3

conn = sqlite3.connect(r"D:\git\bi-an-assisted\data\app.db")
conn.row_factory = sqlite3.Row

print("=== 持仓 ===")
for r in conn.execute(
    "SELECT id, symbol, direction, qty, stop_price, stop_order_id, signal_id, status "
    "FROM positions WHERE status = 'open'"
):
    print(dict(r))

print("=== 剩余信号 ===")
for r in conn.execute("SELECT id, card_json FROM signals ORDER BY created_at"):
    c = json.loads(r["card_json"])
    print(r["id"][:48], "|", c.get("trigger_level"), c.get("direction"),
          "| conf", c.get("confidence"), "| executed", c.get("executed"))
