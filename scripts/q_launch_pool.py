# -*- coding: utf-8 -*-
"""统计启动感知 L1+L2 预筛通过数（dev helper）。"""
import sys

sys.path.insert(0, r"D:\git\bi-an-assisted\backend")

from app.data.fetcher import BinanceFetcher
from app.scan.coarse import CoarseScanner
from app.strategy.launch_sense import prefilter

f = BinanceFetcher()
c = CoarseScanner(f)

pool = c.scan()
print("候选池:", len(pool))
passed = []
for sym in pool:
    daily = f.fetch_ohlcv(sym, "1d", limit=200)
    if daily is None or len(daily) < 181:
        continue
    h1 = f.fetch_ohlcv(sym, "1h", limit=60)
    if h1 is None:
        continue
    res = prefilter(daily, h1)
    if res:
        passed.append(sym)
        l1 = res["layers"]["layer1"]["note"]
        l2 = res["layers"]["layer2"]["note"]
        print("  PASS", sym, "|", l1, "|", l2)
print("L1+L2 通过:", len(passed), "个")
print("小池:", passed)
