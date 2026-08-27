# -*- coding: utf-8 -*-
"""List C-level trigger signals with exec/position linkage (dev helper)."""
import json
import sqlite3

conn = sqlite3.connect(r"D:\git\bi-an-assisted\data\app.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT id, card_json FROM signals ORDER BY created_at").fetchall()
c_sigs = []
for r in rows:
    try:
        card = json.loads(r["card_json"])
    except Exception:
        continue
    if card.get("trigger_level") == "C":
        c_sigs.append((r["id"], card))

print("C 级信号数:", len(c_sigs))
for sig_id, card in c_sigs:
    pos = conn.execute(
        "SELECT id, symbol, status FROM positions WHERE signal_id = ?", (sig_id,)
    ).fetchone()
    print("-", sig_id, "|", card.get("symbol"), card.get("direction"),
          "conf", card.get("confidence"), "| executed:", card.get("executed"),
          "| status:", card.get("status"), "| 关联持仓:", dict(pos) if pos else None)
