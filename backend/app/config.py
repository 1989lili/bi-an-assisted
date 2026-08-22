"""全局配置：所有参数默认值来自 docs/PRODUCT_DESIGN.md §9 参数配置表（v4 定稿）。

后续设置页将支持写入 data/settings.json 覆盖默认值，优先级：settings.json > 本文件。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------- 基础 ----------
# 币安 API 本地代理（大陆直连超时，必配；可在 data/settings.json 中覆盖）
PROXY = "http://127.0.0.1:7892"

# 数据目录与 SQLite 路径
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = DATA_DIR / "app.db"

# ---------- 多周期框架 ----------
TIMEFRAMES = {"direction": "4h", "confirm": "1h", "entry": "15m", "micro": "5m"}
KLINE_LIMIT = 300  # K 线拉取根数默认值
# K 线根数按周期分级：长周期 K 线收盘慢，无需 300 根（EMA55/布林20 余量充足）
TF_KLINE_LIMIT = {"4h": 120, "1h": 200, "15m": 300, "5m": 300}
# K 线缓存 TTL 按周期分级：4h 每 4 小时才收盘一根，缓存久些避免每轮精扫重拉
TF_CACHE_TTL = {"4h": 900, "1h": 600, "15m": 300, "5m": 120}
DEFAULT_CACHE_TTL = 300

# ---------- 指标参数 ----------
EMA_PERIODS = (7, 21, 55)          # 快/中/慢均线
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14
RSI_OVERSOLD = 30                  # 超卖区
RSI_LOW_ZONE = 45                  # 低位区
RSI_CROSS = 50                     # 扳机穿越线
BOLL_PERIOD, BOLL_STD = 20, 2
ATR_PERIOD = 14
VOL_MA_PERIODS = (7, 21)           # 均量线
ADX_PERIOD = 14
ADX_TREND_TH = 25                  # ADX>25 才开方向门
SWING_WINDOW = 5                   # 价格结构枢轴窗口（HH/HL 识别）

# ---------- 量能否决（一票否决） ----------
VOL_RATIO_VETO = 1.2               # 量比 <1.2 且 OI 无增长 → 否决
VOL_RATIO_HOT = 1.5                # 放量阈值
VOL_RATIO_LOW = 0.7                # 缩量阈值
OI_GROWTH_VETO = 0.01              # OI 变化率 <+1% 视为无增长

# ---------- 资金费率（动态两档） ----------
FUNDING_NORMAL_MAX = 0.0003        # 正常档上限 0.03%
FUNDING_STABLE_MAX = 0.001         # 稳定高位档上限 0.1%
FUNDING_SURGE_TIMES = 3.0          # 24h 飙升 ≥3 倍 = 危险档
FUNDING_STABLE_FLUCT = 0.30        # 24h 波动 <±30% = 高位稳定
FUNDING_POSITION_FACTOR = 0.7      # 稳定高位档仓位系数

# ---------- 波动率自适应（布林带宽） ----------
BW_BASE_WINDOW = 100               # 对比过去 N 根 K 线的带宽中位数
BW_NARROW_FACTOR = 0.75            # 带宽 < 中位×0.75 → 收缩
BW_WIDE_FACTOR = 1.5               # 带宽 > 中位×1.5 → 扩张
ATR_COEF_NARROW = 1.0              # 收缩档：紧止损
ATR_COEF_NORMAL = 1.5              # 正常档
ATR_COEF_WIDE = 2.0                # 扩张档：宽止损

# ---------- 风控 ----------
MIN_RISK_REWARD = 2.0              # 盈亏比 ≥2
RISK_PER_TRADE = 0.02              # 单笔风险 = 总资金 2%
LIQ_DIST_BASE = 1.5                # 清算距离 ≥ 波动率系数×ATR

# ---------- 执行 ----------
EXEC_MARKET_PCT = 0.7              # 市价入场 70%
EXEC_LIMIT_PCT = 0.3               # 限价加仓 30%
EXEC_LIMIT_TTL_BARS = 3            # 加仓单 3 根 15m K 线（45 分钟）未成交撤单
SIGNAL_TTL_BARS = 3                # 信号有效期 3 根 15m K 线

# ---------- 止损三段式 ----------
SL_INIT_COEF = 2.0                 # 阶段一：初始止损 2.0×ATR（带宽扩张档）
BE_PROFIT_ATR = 1.5                # 阶段二：浮盈 ≥1.5×ATR → 保本止损
TRAIL_PROFIT_ATR = 3.0             # 阶段三：浮盈 ≥3×ATR → EMA21 动态跟踪

# ---------- 两阶段扫描 ----------
COARSE_INTERVAL_SEC = 60           # 粗筛：每 1 分钟
DEEP_INTERVAL_SEC = 300            # 精扫：每 5 分钟
SIGNAL_MONITOR_INTERVAL_SEC = 60   # 活跃信号高频监控：每 1 分钟（信号级实时跟踪）
SCAN_CONCURRENCY = 4               # 精扫并发度：多币并行拉取（权重预算见 fetcher，双保险防限速）
CANDIDATE_MIN_QUOTE_VOLUME = 1_000_000  # 候选池僵尸币过滤：24h 成交额 < 100万 USDT 不入池（自选除外）
CANDIDATE_TOP_VOLUME = 100         # 候选池：成交额 Top N
CANDIDATE_TOP_CHANGE = 10          # 候选池：|涨跌幅| 异动 Top N
CANDIDATE_TOP_GAIN = 100           # 候选池：涨幅 Top N（强势上涨标的）
DEFAULT_WATCHLIST = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]  # 初始自选

# ---------- 宏观静默期 ----------
MACRO_SILENCE_MINUTES = 15         # 数据公布前后各 15 分钟暂停开仓


# ---------- settings.json 覆盖（优先级：settings.json > 本文件） ----------
def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def load_settings_override() -> None:
    """启动时读取 data/settings.json 中的用户覆盖项，覆盖本模块默认常量。

    仅覆盖「大写命名且已存在」的键，避免写入内部路径/临时键造成损坏。
    文件不存在或解析失败时静默返回（使用默认值）。
    """
    p = _settings_path()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("settings.json 读取失败，使用默认配置")  # noqa: F821
        return
    for k, v in data.items():
        if k.isupper() and k in globals():
            globals()[k] = v


def save_setting(key: str, value: object) -> None:
    """持久化单个配置到 data/settings.json（写入后由 load_settings_override 覆盖生效）。

    只记录用户主动覆盖的键；值可为数值/字符串等 JSON 标量。
    """
    p = _settings_path()
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    data[key] = value
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# 模块导入时立即加载用户覆盖（config 被 import 时生效，main 启动即用最新值）
load_settings_override()
