# 币安 U 本位合约辅助决策工具 — 技术方案设计（v1）

> 状态：待评审 | 日期：2026-08-20
> 依据：docs/PRODUCT_DESIGN.md（v4 定稿），本文档描述技术实现方案。

---

## 1. 技术栈选型

| 层 | 选型 | 理由 |
|----|------|------|
| 语言 | Python 3.11+ | 量化生态成熟（pandas/numpy），币安 SDK 支持好 |
| 后端框架 | FastAPI + uvicorn | 异步高性能，原生 WebSocket 支持（实时推送必需） |
| 币安对接 | ccxt | 活跃维护，统一行情接口；后续扩展交易所成本低 |
| 指标计算 | pandas + numpy | 向量化 K 线计算，11 指标一次批量完成 |
| 任务调度 | APScheduler | 粗筛/精扫/费率刷新等定时任务，支持 cron 表达式 |
| 存储 | SQLite（aiosqlite） | 零部署，单文件，信号历史/持仓/配置足够 |
| 前端 | Vue3 + Vite + Vant | Vant 为移动端组件库，H5 手机兼容首选 |
| 图表 | TradingView lightweight-charts | 轻量专业 K 线图，手机端友好 |
| 实时推送 | WebSocket（FastAPI 原生） | 信号/预警/持仓状态实时到达前端 |

**运行模式**：后端本地启动（uvicorn 常驻），前端由 Vite 构建后由后端托管静态文件，浏览器访问 `http://localhost:8000`；手机与电脑同局域网可访问 `http://<本机IP>:8000`。

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────┐
│ 前端 H5（Vue3 + Vant）                                    │
│ 手机/桌面浏览器: 信号流 | 自选币 | 持仓 | 复盘 | 设置        │
└───────────────┬─────────────────────────────────────────┘
                │ REST API（读/写）+ WebSocket（实时推送）
┌───────────────▼─────────────────────────────────────────┐
│ 后端 FastAPI                                              │
│ ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│ │ 行情采集器 │→ │ 指标引擎  │→ │ 信号引擎  │→ │ 通知服务    │  │
│ │ fetcher   │  │ 11指标   │  │ 10关卡   │  │ WS广播+声音 │  │
│ └──────────┘  └──────────┘  └──────────┘  └───────────┘  │
│ ┌──────────┐  ┌──────────┐  ┌─────────────────────────┐  │
│ │ 扫描调度器 │  │ 持仓管理器 │  │ SQLite(信号/持仓/费率历史/  │  │
│ │ 粗筛/精扫  │  │ 止损三段式 │  │ 宏观日历/配置)            │  │
│ └──────────┘  └──────────┘  └─────────────────────────┘  │
└───────────────┬─────────────────────────────────────────┘
                │ ccxt REST（行情只读，无需 API Key）
                ▼
        币安 U 本位合约市场
```

**数据流（精扫一轮）**：
```
APScheduler 触发 → 粗筛(24hr ticker 全市场) → 候选池
→ 精扫(候选池 K线/费率/OI) → 指标引擎计算 → 信号引擎 10 关卡
→ 通过/降级 → 信号落库 → WS 推送前端 → 声音+浏览器通知
→ 信号进入生命周期监控（有效期/失效/成交）
```

## 3. 币安 API 对接明细（U 本位合约，行情只读）

| 数据 | ccxt 方法 / 接口 | 频率 | weight | 用途 |
|------|-----------------|------|--------|------|
| 全市场 24h 统计 | `fetch_tickers()`（/fapi/v1/ticker/24hr） | 每 1 分钟 | ~40 | 粗筛候选池 |
| K线 | `fetch_ohlcv()`（/fapi/v1/klines） | 每 5 分钟 × 候选池 × 4周期 | ~240/轮 | 全部指标计算 |
| 资金费率 | `fetch_funding_rate()`（/fapi/v1/premiumIndex） | 每 4 小时 | ~10 | 费率 ROC 两档判断 |
| 持仓量历史 | /futures/data/openInterestHist | 每 5 分钟 × 候选池 | ~20/轮 | OI 变化率 |
| 多空比 | /futures/data/globalLongShortAccountRatio | 每 5 分钟 × 候选池 | ~20/轮 | 情绪参考 |
| 清算数据 | forceOrders（受限）→ 第一版用估算 | 每 5 分钟 | - | 清算密集区距离 |

**说明**：
- 全部行情接口**无需 API Key**（只读），无密钥泄露风险
- K线 limit 取 300 根（EMA55/布林20 等指标计算余量充足）
- weight 预算：约 290/分钟，远低于上限 2400/分钟，余量支持候选池扩至 100 币
- **清算数据**：币安 forceOrders 公开接口受限，第一版用"OI 分布 + 杠杆分段估算密集区"近似，二版再评估第三方数据源
- 网络容错：指数退避重试（3 次），失败降级（该轮跳过，不阻塞后续轮次）

## 4. 核心模块设计

### 4.1 行情采集器（data/fetcher.py + cache.py）
- 职责：封装 ccxt 调用、响应缓存（TTL 内存缓存，防重复请求）
- 数据结构：`Kline[symbol][timeframe]`、`Ticker24h[]`、`FundingRate[]`、`OIH[]`
- K线窗口：内存保留 300 根/币/周期，费率历史保留 24h（算 ROC）

### 4.2 指标引擎（indicators/engine.py）
- 输入：K线 DataFrame；输出：指标快照（EMA7/21/55、MACD、RSI、VOL MA7/21、量比、ATR、布林带宽、ADX、价格结构、OI 变化率、费率档位）
- 纯函数设计（输入 K线 → 输出快照），便于单元测试与回测复用
- 价格结构（HH/HL）：Swing 高低点识别（枢轴窗口 = 5 根）

### 4.3 信号引擎（signal/engine.py + scorer.py）
- 逐层执行 10 关卡（见产品文档第 4 节），任一层否决即终止（记录否决原因）
- 输出信号对象 `SignalCard`，结构：

```json
{
  "id": "sig_20260820_1432_BTCUSDT",
  "symbol": "BTCUSDT", "direction": "long",
  "confidence": 82,
  "levels": {
    "market_env": true, "direction_gate": true,
    "trigger": "A", "volume_veto": true,
    "risk_brake": true, "candle_check": "normal",
    "macro_silence": true
  },
  "funding": {"rate": 0.0006, "tier": "stable_high", "position_factor": 0.7},
  "execution": {
    "market_pct": 70, "limit_price": 67200.0, "limit_pct": 30,
    "stop_loss": 66600.0, "risk_reward": 2.4
  },
  "reason": "15m RSI 上穿50 + 5m MACD柱翻红，量比1.8，OI +3.5%",
  "created_at": "2026-08-20T14:32:00Z",
  "expires_at": "2026-08-20T15:17:00Z"
}
```

### 4.4 持仓管理器（position/manager.py）
- 状态机：`开仓 → 初始止损 → 保本止损 → EMA21跟踪 → 平仓`
- 输入：手动录入开仓价/方向/数量；输出：当前阶段、当前止损价、建议动作
- 时间止损、相关性检查、出场预警触发条件在此模块评估

### 4.5 扫描调度器（scan/coarse.py + deep.py）
- 粗筛（1 分钟）：fetch_tickers → 成交额 Top N ∪ 涨跌幅异动 Top N ∪ 自选池
- 精扫（5 分钟）：候选池 → K线/费率/OI → 指标引擎 → 信号引擎
- 候选池变更日志（记录"新增进池"事件，用于扫描报告）

### 4.6 宏观日历（calendar/macro.py）
- 内置高频事件表（CPI/非农/FOMC/鲍威尔讲话的常规时间）
- 用户手动维护（设置页增删改，存 SQLite）
- 静默期判定：`now ∈ [事件时间-15min, 事件时间+15min]`

### 4.7 通知服务（notify/ws.py）
- WebSocket 事件协议（服务器→客户端）：

| 事件 | 载荷 |
|------|------|
| `signal:new` | SignalCard |
| `signal:expired` | signal_id |
| `alert:new` | 预警对象（类型/级别/建议） |
| `scan:report` | 扫描报告 |
| `position:update` | 持仓状态 |
| `status:update` | 连接/静默期/费率状态 |

- 客户端→服务器：`watchlist:add/remove`、`position:create/update/close`、`settings:update`
- 浏览器通知：前端监听 WS 事件 → Notification API + 声音

### 4.8 存储（store/db.py）
- 表：`signals`（信号卡全量）、`positions`（持仓）、`funding_history`（费率 ROC）、`watchlist`、`macro_events`、`settings`（KV 配置）、`scan_log`（候选池变更）

## 5. 前端设计（H5 响应式）

| 页面 | 功能 |
|------|------|
| 信号流（首页） | 信号卡片流（置顶动画）、预警卡、扫描报告卡片 |
| 自选币 | 概览表 + 展开详情（11 指标读数 + KDJ 温度计 + 迷你K线） |
| 持仓 | 持仓卡片（止损阶段/当前止损/建议）、手动录入开仓 |
| 复盘 | 信号历史列表 + 胜率/盈亏统计图表 |
| 设置 | 全部参数（见产品文档 §9）、宏观日历维护、自选池管理 |

- 断线重连：WS 自动重连（指数退避），重连后拉取最近 100 条信号补偿
- 手机适配：底部 Tab 导航、卡片单列、横向滚动表格

## 6. 项目目录结构

```
bi-an-assisted/
├── docs/
│   ├── PRODUCT_DESIGN.md      # 产品设计（已定稿）
│   └── TECH_DESIGN.md         # 本文件
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 入口 + 静态托管 + WS 路由
│   │   ├── config.py          # 参数配置（默认值 = 产品文档 §9）
│   │   ├── scheduler.py       # APScheduler 任务注册
│   │   ├── data/
│   │   │   ├── fetcher.py     # ccxt 封装（重试/缓存）
│   │   │   └── cache.py       # TTL 内存缓存
│   │   ├── indicators/
│   │   │   └── engine.py      # 11 指标纯函数计算
│   │   ├── signal/
│   │   │   ├── engine.py      # 10 关卡流水线
│   │   │   └── scorer.py      # 置信度打分
│   │   ├── scan/
│   │   │   ├── coarse.py      # 粗筛
│   │   │   └── deep.py        # 精扫
│   │   ├── position/
│   │   │   └── manager.py     # 持仓状态机
│   │   ├── calendar/
│   │   │   └── macro.py       # 宏观日历 + 静默期
│   │   ├── notify/
│   │   │   └── ws.py          # WS 广播
│   │   ├── store/
│   │   │   └── db.py          # SQLite
│   │   └── api/
│   │       └── routes.py      # REST 路由
│   ├── tests/                 # 指标/信号引擎单元测试（历史数据）
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── main.js
│   │   ├── router.js
│   │   ├── api/ws.js          # WS 客户端（断线重连）
│   │   ├── views/             # SignalFeed / Watchlist / Positions / Review / Settings
│   │   └── components/        # SignalCard / AlertCard / PositionCard / ScanReport ...
│   ├── package.json
│   └── vite.config.js
├── data/                      # SQLite 数据文件（运行时生成）
└── README.md
```

## 7. 开发里程碑

| 里程碑 | 内容 | 验收标准 |
|--------|------|---------|
| M1 数据层 | fetcher + 缓存 + 粗筛/精扫调度 + SQLite | 后台能持续产出候选池与指标快照 |
| M2 指标与信号 | 11 指标计算 + 10 关卡引擎 + 打分 | 单元测试通过（历史数据回放验证） |
| M3 后端服务 | FastAPI + WS + 通知 + 持仓管理器 | curl/WS 客户端能收到完整 SignalCard |
| M4 前端 H5 | Vue3 + Vant 五页面 + WS 对接 | 手机/桌面浏览器完整可用 |
| M5 打磨 | 宏观静默、复盘统计、参数调优、README | 全流程可用，交付验收 |

## 8. 风险与对策

| 风险 | 对策 |
|------|------|
| 清算数据不可用（接口受限） | 第一版 OI 分布估算密集区，产品文档标注为近似值 |
| 指标参数需实战调优 | 全部参数配置化（设置页），复盘统计支持 A/B 参数对比 |
| 币安接口限流/故障 | 指数退避重试 + 轮次降级跳过 + 缓存兜底 |
| K线收盘确认延迟（≤15分钟） | 已设计"旱地拔葱"例外通道 |
| 手机端通知依赖浏览器 | 浏览器通知 + 声音；二版评估 PWA 或外部推送 |
| 费率 ROC 数据冷启动 | 启动后持续记录费率历史，24h 内用绝对值降级判断 |

## 9. 待评审项

1. 技术栈（Python FastAPI + Vue3/Vant + ccxt）是否认可？
2. 清算数据第一版用估算方案是否接受？
3. 里程碑 M1→M5 顺序是否合理？
