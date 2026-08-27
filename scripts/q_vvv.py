# -*- coding: utf-8 -*-
"""Check VVV signal execution price (dev helper)."""
import json
import sqlite3

conn = sqlite3.connect(r"D:\git\bi-an-assisted\data\app.db")
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT id, card_json FROM signals WHERE id LIKE '%VVV%'").fetchone()
if row:
    card = json.loads(row["card_json"])
    print("id =", row["id"])
    print("execution =", json.dumps(card.get("execution"), indent=1))
    print("executed =", card.get("executed"))
else:
    print("VVV signal not found")
