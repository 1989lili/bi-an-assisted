# 币安 U 本位合约辅助决策工具（bi-an-assisted）

本地运行的加密货币**辅助决策**工具。监听 Binance U 本位合约行情，用一套多周期指标 + 信号流水线，实时给出**入场信号卡、持仓风控建议与出场预警**。手机/桌面浏览器均可访问。

> **核心原则：只做决策辅助，从不自动下单。** 信号仅作参考，实际交易由你自己执行。
>
> 产品设计依据：`docs/PRODUCT_DESIGN.md`（v4 定稿）· 技术方案：`docs/TECH_DESIGN.md`

---

## 功能概览

- **信号流（首页）**：实时入场信号卡，置顶动画 + 高频价格/状态监控；已过期/止损信号沉底分组展示。
- **自选币**：概览 + 展开单币详情（多周期指标、费率档位、OI 变化率、清算距离）。
- **持仓**：手动录入开仓，自动按「止损三段式」计算阶段与当前止损价、建议动作。
- **复盘**：信号历史列表 + 统计。
- **设置**：宏观静默期日历维护、引擎参数调优、连接状态。

信号流水线为「10 层关卡」：市场环境 → 方向门 → 双周期扳机 → 量能否决 → 风控刹车 → K 线形态 → 宏观静默 → 仓位与执行 → 持仓管理 → 信号生命周期；另有平行的出场预警引擎。

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.11+ · FastAPI + uvicorn · ccxt · pandas/numpy · APScheduler · SQLite |
| 前端 | Vue3 + Vite + Vant（移动端 H5） |
| 推送 | WebSocket（信号 / 预警 / 持仓 / 扫描报告实时到达） |

## 快速开始

### 1. 后端

```bash
cd backend
# 用 py12 环境安装依赖
D:\miniconda3\envs\py12\python.exe -m pip install -r requirements.txt
# 启动（首次启动即执行首轮精扫）
D:\miniconda3\envs\py12\python.exe -m app.main
```

默认监听 `http://localhost:8000`；手机同局域网可访问 `http://<本机IP>:8000`。

> 行情走 ccxt（只读，无需 API Key），通过本地代理直连（默认 `http://127.0.0.1:7892`，可在
> `data/settings.json` 覆盖 `PROXY`）。

### 2. 前端

开发模式：

```bash
cd frontend
npm install
npm run dev        # Vite 开发服务器
```

生产构建（构建产物由后端 `main.py` 静态托管）：

```bash
npm run build      # 生成 frontend/dist
```

### 3. 定时任务

后端启动时经 APScheduler 注册并运行：

- 粗筛：每 60s（全市场 24h ticker → 候选池）
- 精扫：每 300s（候选池 K 线 / 费率 / OI → 指标 → 信号引擎）
- 信号监控：每 60s（活跃信号高频价格 / 状态更新）

## 项目结构

```
bi-an-assisted/
├── docs/                # 产品/技术设计文档 + 每日开发日志
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI 入口 + 静态托管 + WS
│   │   ├── config.py        # 全局参数（默认 <- settings.json 覆盖）
│   │   ├── scheduler.py     # 定时任务
│   │   ├── data/            # fetcher（ccxt）+ cache
│   │   ├── indicators/      # 11 指标纯函数计算
│   │   ├── signal/          # 信号引擎（10 关卡）+ 打分 + 监控
│   │   ├── scan/            # 粗筛 / 精扫
│   │   ├── position/        # 持仓状态机（止损三段式）
│   │   ├── calendar/        # 宏观静默期内置事件表
│   │   ├── notify/          # WebSocket 广播
│   │   ├── store/           # SQLite 存储
│   │   └── api/             # REST 路由
│   ├── tests/               # 单元测试
│   └── requirements.txt
├── frontend/            # Vue3 + Vant H5
├── data/                # SQLite 数据 + settings.json（运行时生成）
└── README.md
```

## 参数配置

所有参数默认值在 `backend/app/config.py`。用户可在「设置」页调整，写入 `data/settings.json`，
启动时覆盖默认值（优先级：`settings.json > config.py`）。重启服务后仍保留。

## 其它

- 时间显示为本地时区，内部存储 UTC。
- 数据全部落盘 SQLite（`data/app.db`）：信号历史 / 持仓 / 费率历史 / 宏观日历 / 配置 / 扫描日志。
- **免责声明**：本工具仅供学习与研究，不构成投资建议；加密货币合约波动剧烈，请自行评估风险。
