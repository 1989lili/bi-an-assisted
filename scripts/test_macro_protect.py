# -*- coding: utf-8 -*-
"""PositionMonitor._macro_protect 单测（mock fetcher/executor，dev helper）。"""
import numpy as np
import pandas as pd

import app.config as config
import app.store.db as db
from app.position.monitor import PositionMonitor


class FakeFetcher:
    def fetch_ohlcv(self, symbol, tf, limit=60):
        n = limit or 60
        close = np.linspace(95, 100, n)
        return pd.DataFrame({
            "ts": pd.date_range(end=pd.Timestamp.utcnow(), periods=n, freq="15min"),
            "open": close, "high": close + 1.5, "low": close - 1.5,
            "close": close, "volume": np.full(n, 1000.0),
        })


class FakeExecutor:
    configured = True

    def __init__(self):
        self.calls = []

    def amount_to_precision(self, sym, amt):
        return round(amt, 6)

    def min_amount(self, sym):
        return 0.001

    def create_order(self, *a, **k):
        self.calls.append(("order", a, k))
        return {"ok": True, "id": "o1"}

    def cancel_order(self, *a, **k):
        self.calls.append(("cancel", a, k))
        return {"ok": True}

    def create_stop_loss_order(self, *a, **k):
        self.calls.append(("stop", a, k))
        return {"ok": True, "id": "s2"}


updates = []


def fake_update(pid, **kw):
    updates.append((pid, kw))
    return True


db.update_position = fake_update

mon = PositionMonitor(FakeFetcher(), FakeExecutor())
executor = mon.executor

# 场景1：多头浮盈，旧止损95 现价~100 ATR~1.5 → 新止损 = max(95, 100-0.75)≈99.25
updates.clear(); executor.calls.clear()
mon._macro_protect({"id": 1, "symbol": "BTC/USDT:USDT", "direction": "long", "qty": 0.1,
                    "entry_price": 97, "stop_price": 95.0, "stop_order_id": "stop1", "macro_protected": 0})
print("场景1 多头收紧:", updates[-1])
print("  交易所操作:", executor.calls)

# 场景2：空头浮亏，旧止损105 → 新止损 = min(105, 100+0.75)≈100.75
updates.clear(); executor.calls.clear()
mon._macro_protect({"id": 2, "symbol": "BTC/USDT:USDT", "direction": "short", "qty": 0.1,
                    "entry_price": 102, "stop_price": 105.0, "stop_order_id": "stop2", "macro_protected": 0})
print("场景2 空头收紧:", updates[-1])

# 场景3：已保护 → 不重复执行
updates.clear(); executor.calls.clear()
mon._macro_protect({"id": 3, "symbol": "BTC/USDT:USDT", "direction": "long", "qty": 0.1,
                    "entry_price": 97, "stop_price": 95.0, "stop_order_id": None, "macro_protected": 1})
print("场景3 已保护(应无更新):", updates, executor.calls)

# 场景4：减仓50%（无旧止损单）+ 收紧
config.MACRO_SILENCE_REDUCE_PCT = 0.5
updates.clear(); executor.calls.clear()
mon._macro_protect({"id": 4, "symbol": "BTC/USDT:USDT", "direction": "long", "qty": 0.1,
                    "entry_price": 97, "stop_price": 95.0, "stop_order_id": None, "macro_protected": 0})
print("场景4 减仓+收紧:", updates[-1])
print("  交易所操作:", executor.calls)
config.MACRO_SILENCE_REDUCE_PCT = 0.0

# 场景5：减仓后剩余不足最小量 → 放弃减仓仅收紧
config.MACRO_SILENCE_REDUCE_PCT = 0.9
updates.clear(); executor.calls.clear()
mon._macro_protect({"id": 5, "symbol": "BTC/USDT:USDT", "direction": "long", "qty": 0.001,
                    "entry_price": 97, "stop_price": 95.0, "stop_order_id": None, "macro_protected": 0})
print("场景5 剩余不足(应无减仓单):", updates[-1], "| order调用:", [c[0] for c in executor.calls])
config.MACRO_SILENCE_REDUCE_PCT = 0.0
print("ALL_SCENARIOS_DONE")
