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
RSI_BOUNCE_CROSS = 40              # A级回踩 RSI 回升穿越线（多头 <40→≥40；空头 >60→≤60，超卖/超买拐头用 30/70）
BOLL_PERIOD, BOLL_STD = 20, 2
ATR_PERIOD = 14
VOL_MA_PERIODS = (7, 21)           # 均量线
VOL_MA_WINDOW = 14                 # 量比/缩量/放量基准窗口（原 20 均量口径统一改 14）
ADX_PERIOD = 14
ADX_TREND_TH = 30                  # ADX>30 才开方向门（实验收紧：20→30；设置页可调，可回退）
SCORE_PASS = 60                    # 信号置信度通过线（原 70 收紧后信号过少，下调 60 观察；可再调）
CANDLE_EXCEPTION_ATR_MULT = 2.0    # 旱地拔葱：单根 K 线实体涨幅 > 2×ATR
CANDLE_EXCEPTION_WICK_RATIO = 0.2  # 旱地拔葱：收盘贴近极值——影线 < 0.2×实体（多头看上影/空头看下影）
SWING_WINDOW = 5                   # 价格结构枢轴窗口（HH/HL 识别）
# 扳机动量确认放宽（N0.1）：5m MACD 柱同号连续根数 ≤ 该值视为有效
# 1 = 仅"刚翻色"（原逻辑，最严）；3 = 翻色后 3 根内延续（默认）；5 = 激进
TRIGGER_MOMENTUM_BARS = 3
# 【临时放开】C 级扳机（RSI 穿越 50 无量能，原"只观察"拦截）：
# 为放出几个信号观察信号量，临时放行；观察完改回 False 收紧。
TRIGGER_C_LEVEL_ALLOW = True

# ---------- 策略一：EMA 趋势跟踪（N0.7，适合单边行情） ----------
EMA_TREND_TIMEFRAMES = {"trend": "4h", "confirm": "1h", "entry": "15m"}  # 4h 方向 + 1h 中期确认 + 15m 入场
EMA_TREND_FAST, EMA_TREND_MID, EMA_TREND_LONG = 20, 50, 200  # 短/中/长均线
EMA_TREND_VOL_MULT = 2.0           # 放量阈值（收紧 1.5→2.0，放量更明确）
EMA_TREND_MIN_SLOPE_PCT = 0.3      # EMA200 斜率阈值 %（收紧：仅 >0 改 >0.3%，排除伪趋势）
EMA_TREND_RETRACE_LOOKBACK = 5     # 回踩判定：近 N 根内曾触及 EMA20
EMA_TREND_ENTRY_NEAR_ATR = 2.0     # 入场价与 EMA20 距离 ≤ 2×ATR（不追远离均线）
EMA_TREND_RSI_MIN, EMA_TREND_RSI_MAX = 50, 68  # 多头 RSI 50~68（修漏洞：此前 32~68 会让偏空 RSI 出多头）
EMA_TREND_RSI_SHORT_MIN = 32                   # 空头 RSI 下界（空头区间 32~50）
# 策略一出场（三层，monitor 判定，收盘价为准）
EMA_TREND_EXIT_ATR = 3.0           # ① 吊灯止损：持仓期最高/最低收盘价 ∓ N×ATR
EMA_TREND_TP_RR = 2.5              # 第一目标止盈盈亏比：止盈价 = 入场价 ± RR×止损距离
EMA_TREND_TIME_BARS = 48           # ③ 时间止损：入场后 N 根入场周期 K 线未创新高/新低 → 离场

# ---------- 量能否决（一票否决） ----------
VOL_RATIO_VETO = 1.2               # 量比 <1.2 且 OI 无增长 → 否决
VOL_RATIO_HOT = 1.5                # 放量阈值
VOL_RATIO_LOW = 0.7                # 缩量阈值
OI_GROWTH_VETO = 0.01              # OI 变化率 <+1% 视为无增长

# ---------- 量能正向加分（N0.5，量价配合进打分） ----------
VOL_SCORE_STRONG = 8               # 放量+OI 增长：强确认（真突破）
VOL_SCORE_MILD = 4                 # 仅放量或仅 OI 增 / 缩量回踩蓄势（A 级场景）

# ---------- 波动率目标仓位（N0.5，vol targeting） ----------
VOL_TARGET_ATR_PCT = 0.008         # 目标单根波动（ATR/价格），实际波动高于此则降仓
VOL_FACTOR_MIN = 0.5               # 波动率仓位系数下限（最多降一半）
VOL_FACTOR_MAX = 1.0               # 上限（不放大仓位）

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

# ---------- N1 执行层（币安 U 本位交易，默认纸面模式） ----------
BINANCE_API_KEY = ""               # 由 data/settings.json 覆盖（用户自行填写）
BINANCE_API_SECRET = ""            # 敏感字段：GET /api/settings 不外泄
BINANCE_DRY_RUN = True             # 纸面模式（默认开）；确认小额实盘后再置 False
BINANCE_MAX_ORDER_USDT = 100       # 单笔下单金额上限（USDT，防误操作超仓）
BINANCE_MAX_LEVERAGE = 5           # 交易杠杆上限（≤5 倍，不允许超过）
BINANCE_DEFAULT_LEVERAGE = 3       # 一键执行默认杠杆（确认页可选 1~5）
BINANCE_RISK_LEVERAGE = 3          # 风控评估假设杠杆（预估强平价用，默认 3）
BINANCE_MAINT_MARGIN_RATE = 0.005  # U 本位维持保证金率估算（0.5%）
BINANCE_MIN_NOTIONAL = 5           # 币安 U 本位最小名义价值（USDT，开仓单必须 ≥5）
BINANCE_DAILY_OPEN_LIMIT = 5       # 每日最大开仓次数
BINANCE_DAILY_LOSS_LIMIT = 0.05    # 单日亏损熔断（占总资金比例）

# ---------- 访问鉴权（H6） ----------
APP_AUTH_TOKEN = ""                # 访问令牌（空=不启用鉴权；设置后所有 /api 需 Authorization: Bearer <token>）

# ---------- 执行 ----------
EXEC_MARKET_PCT = 0.7              # 市价入场 70%
EXEC_LIMIT_PCT = 0.3               # 限价加仓 30%
EXEC_DEFAULT_BUDGET_PCT = 0.5      # 一键执行默认预算 = 总余额 × 50%（确认页可调整）
EXEC_LIMIT_TTL_BARS = 3            # 加仓单 3 根 15m K 线（45 分钟）未成交撤单
SIGNAL_TTL_BARS = 3                # 信号有效期 3 根 15m K 线
SIGNAL_COOLDOWN_MINUTES = 30       # 信号冷却：同 symbol/direction/strategy 止损/过期后 30 分钟内不重生成（H8 去重）

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
MACRO_SILENCE_STOP_ATR = 0.5       # 静默窗口内持仓止损收紧到现价 0.5×ATR 距离（只紧不松）
MACRO_SILENCE_REDUCE_PCT = 0.0     # 静默窗口内持仓减仓比例（0=只收紧止损不减仓；0.5=减半仓）


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
