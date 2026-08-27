# -*- coding: utf-8 -*-
"""清空 C 级扳机信号（用户指令）：删除 trigger_level='C' 的信号卡；
已执行且关联持仓的，同步置空 positions.signal_id（持仓保留、继续自动风控）。"""
import json
import sqlite3

conn = sqlite3.connect(r"D:\git\bi-an-assisted\data\app.db")
conn.row_factory = sqlite3.Row

c_ids = []
for r in conn.execute("SELECT id, card_json FROM signals"):
    try:
        card = json.loads(r["card_json"])
    except Exception:
        continue
    if card.get("trigger_level") == "C":
        c_ids.append(r["id"])

if not c_ids:
    print("无 C 级信号")
else:
    placeholders = ",".join("?" * len(c_ids))
    # 关联持仓解除 signal_id 引用
    linked = conn.execute(
        f"SELECT id FROM positions WHERE signal_id IN ({placeholders})", c_ids
    ).fetchall()
    if linked:
        conn.execute(
            f"UPDATE positions SET signal_id = NULL WHERE signal_id IN ({placeholders})", c_ids
        )
        print("解除持仓 signal_id:", [d["id"] for d in linked])
    cur = conn.execute(f"DELETE FROM signals WHERE id IN ({placeholders})", c_ids)
    conn.commit()
    print(f"已删除 C 级信号 {cur.rowcount} 条:", c_ids)

print("剩余信号数:", conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0])
