# -*- coding: utf-8 -*-
"""Diagnose: where do signals get rejected across the pool (dev helper)."""
from collections import Counter

from app.data.fetcher import BinanceFetcher
from app.scan.coarse import CoarseScanner
from app.scan.deep import DeepScanner

f = BinanceFetcher()
c = CoarseScanner(f)
d = DeepScanner(f, c)

pool = c.scan()
print("pool_size =", len(pool))

# sample first N symbols to keep runtime bounded
sample = pool[:40]
reasons = Counter()
ok = 0
for sym in sample:
    d._scan_symbol(sym, {})
    r = d.engine.rejections.get(sym)
    if r is None:
        ok += 1
    else:
        reasons[r] += 1

print("sampled =", len(sample), "| signals_ok =", ok)
print("--- rejection distribution (top 15) ---")
for reason, n in reasons.most_common(15):
    print(f"{n:4d}  {reason}")
