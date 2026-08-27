# -*- coding: utf-8 -*-
"""Query current signal stats by strategy (dev helper)."""
import os
import sqlite3

base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
conn = sqlite3.connect(os.path.join(base, "data", "app.db"))
cur = conn.cursor()

cur.execute(
    "SELECT CASE WHEN id LIKE '%_ema%' THEN 'ema_trend(策略一)' ELSE 'short(套1)' END AS strategy, "
    "COUNT(*), SUM(CASE WHEN status='active' THEN 1 ELSE 0 END), MAX(created_at) "
    "FROM signals GROUP BY strategy"
)
rows = cur.fetchall()
print("strategy            | total | active | last_signal_at")
for r in rows:
    print(r)

cur.execute("SELECT COUNT(*) FROM signals WHERE created_at > datetime('now','-30 minutes')")
print("last 30min signals =", cur.fetchone()[0])

cur.execute(
    "SELECT status, COUNT(*) FROM signals WHERE created_at > datetime('now','-30 minutes') "
    "GROUP BY status"
)
print("last 30min by status:", cur.fetchall())
conn.close()
