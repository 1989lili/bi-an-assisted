# -*- coding: utf-8 -*-
"""启动感知三层漏斗单测（合成数据，dev helper）。"""
import numpy as np
import pandas as pd

from app.strategy import launch_sense as ls


def make_df(n, start=100.0, step=0.001, vol=1000.0, tf_min=5):
    """合成 K 线：平稳上涨 + 可调波动。"""
    closes = start * np.cumprod(1 + step) if False else start + np.arange(n) * step * start
    ts = (pd.Timestamp.utcnow().value // 10**9) * 1000
    ts_arr = [ts - (n - 1 - i) * tf_min * 60_000 for i in range(n)]
    return pd.DataFrame({
        "ts": ts_arr,
        "open": closes, "high": closes * 1.002, "low": closes * 0.998,
        "close": closes, "volume": [float(vol)] * n,
        "taker_buy": [float(vol)] * n,
    })


def test_layer1():
    # 底部区域：180 天下跌后低位横盘 → 价<MA180 且空间<50%
    n = 220
    closes = np.linspace(200, 100, n)  # 单边下跌到底部
    df = pd.DataFrame({"ts": list(range(n)), "open": closes, "high": closes * 1.01,
                       "low": closes * 0.99, "close": closes, "volume": [1000.0] * n})
    r = ls.layer1_daily(df)
    print("L1 底部:", r["pass"], "|", r["note"])
    # 高位：上涨后高位 → 空间>50%
    closes2 = np.linspace(100, 190, n)
    df2 = pd.DataFrame({"ts": list(range(n)), "open": closes2, "high": closes2 * 1.01,
                        "low": closes2 * 0.99, "close": closes2, "volume": [1000.0] * n})
    r2 = ls.layer1_daily(df2)
    print("L1 高位:", r2["pass"], "|", r2["note"])


def test_layer2():
    df = make_df(60, start=100.0)
    r = ls.layer2_bias(df)
    print("L2 BIAS(≈0%):", r["pass"], "|", r["note"])
    # 构造 BIAS 6%：MA24 之后急拉 6%
    closes = np.linspace(100, 100, 40).tolist() + [106.0] * 20
    df2 = pd.DataFrame({"ts": list(range(60)), "open": closes, "high": [c * 1.01 for c in closes],
                        "low": [c * 0.99 for c in closes], "close": closes, "volume": [1000.0] * 60})
    r2 = ls.layer2_bias(df2)
    print("L2 BIAS(≈6%):", r2["pass"], "|", r2["note"])


def test_layer3():
    # 构造量能爆发：最后 3 根 taker 放量 3000（MA20≈1000），且 3 根维持（2500~3000）
    df5 = make_df(120, start=100.0, vol=1000.0)
    taker = df5.copy()
    taker["taker_buy"] = [1000.0] * (len(taker) - 3) + [3000.0, 2800.0, 2600.0]
    rv = ls._trigger_volume(taker, len(taker) - 2)
    print("③① 量能:", rv["pass"], "|", rv["note"])

    # 波动率：先收口（窄）后 ATR 跳升 + 价破上轨
    df5b = make_df(120, start=100.0, vol=1000.0)
    n = len(df5b)
    # 让最后 20 根波动骤增
    df5b.loc[df5b.index[-20:], "high"] = df5b.loc[df5b.index[-20:], "close"] * 1.03
    df5b.loc[df5b.index[-20:], "low"] = df5b.loc[df5b.index[-20:], "close"] * 0.97
    df5b.loc[df5b.index[-1], "close"] = df5b["close"].iloc[-1] * 1.02
    rv2 = ls._trigger_volatility(df5b, n - 2)
    print("③② 波动率:", rv2["pass"], "|", rv2["note"])

    # 均线抬头：构造 A/B/C/D（低位 + 站上 MA7/25 + 金叉 + 斜率）
    closes = np.linspace(100, 102, 120)  # 前低后缓升
    closes = closes.tolist()
    closes[-2] = 104.0
    closes[-1] = 106.0  # 当前大涨 → MA7 斜率 + 站上均线
    df5c = pd.DataFrame({"ts": list(range(120)), "open": closes, "high": [c * 1.01 for c in closes],
                         "low": [c * 0.99 for c in closes], "close": closes, "volume": [1000.0] * 120})
    rv3 = ls._trigger_ma(df5c, 118)
    print("③③ 均线:", rv3["pass"], "|", rv3["note"])


def _real_ts(n: int, tf_min: int = 5) -> list:
    """真实时间戳序列（最后一根=now，模拟当前未收盘）。"""
    import time as _t

    now = int(_t.time() * 1000)
    return [now - (n - 1 - i) * tf_min * 60_000 for i in range(n)]


def test_evaluate():
    """精确构造三层全通过 → 应触发。"""
    n5 = 120
    # 5m：前 90 根下跌 100→90，后 30 根反弹（构造金叉+斜率+低位+收口）
    base = np.linspace(100, 90, 90).tolist()
    rebound = [90 + i * 0.45 for i in range(1, 31)]   # 90 → 103.05
    closes5 = base + rebound
    assert len(closes5) == 120
    # 当前验证根 t=118（已收盘 103.2）；119 为未收盘实时（ATR_NOW/上轨用）
    closes5[117] = 102.5   # t=117
    closes5[118] = 104.0   # t=118 收盘：站上均线 + 斜率
    closes5[119] = 104.5   # 未收盘实时
    highs = [c * 1.012 for c in closes5]
    lows = [c * 0.988 for c in closes5]
    df5 = pd.DataFrame({"ts": _real_ts(n5, 5), "open": closes5, "high": highs,
                        "low": lows, "close": closes5, "volume": [1000.0] * n5})
    taker = df5.copy()
    # 后 3 根（116,117,118）taker 放量维持：3000/2800/2600；119 未收盘也高
    tv = [1000.0] * (n5 - 4) + [3000.0, 2800.0, 2600.0, 2400.0]
    taker["taker_buy"] = tv
    # 1h：横盘 50 根 100 + 后 10 根 107 → BIAS≈4%
    closes1h = [100.0] * 50 + [107.0] * 10
    df1h = pd.DataFrame({"ts": _real_ts(60, 60), "open": closes1h, "high": [c * 1.01 for c in closes1h],
                         "low": [c * 0.99 for c in closes1h], "close": closes1h, "volume": [1000.0] * 60})
    # 1d：180 天下跌后低位 → 价<MA180 且空间<50%
    n1d = 220
    closes1d = np.linspace(200, 100, n1d)
    df1d = pd.DataFrame({"ts": _real_ts(n1d, 1440), "open": closes1d, "high": closes1d * 1.01,
                         "low": closes1d * 0.99, "close": closes1d, "volume": [1000.0] * n1d})
    klines = {"1d": df1d, "1h": df1h, "5m": df5}
    r = ls.evaluate(klines, None, taker)
    print("evaluate:", "触发 ✅" if r else "未触发",
          "|", r["reason"] if r else "")
    if r:
        for k, v in r["layers"].items():
            print("  ", k, v["pass"], "|", v["note"])
    return bool(r)


if __name__ == "__main__":
    test_layer1()
    test_layer2()
    test_layer3()
    test_evaluate()
