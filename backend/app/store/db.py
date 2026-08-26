"""SQLite 存储层：信号/持仓/费率历史/自选币/宏观日历/设置/扫描日志。"""
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    direction   TEXT NOT NULL,
    confidence  INTEGER,
    card_json   TEXT NOT NULL,           -- 完整 SignalCard JSON（M2 使用）
    status      TEXT DEFAULT 'active',   -- active/expired/filled/cancelled
    executed    INTEGER DEFAULT 0,       -- 1=已一键执行（幂等占位，H3）
    created_at  TEXT NOT NULL,
    expires_at  TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    direction   TEXT NOT NULL,
    entry_price REAL NOT NULL,
    qty         REAL NOT NULL,
    stop_stage  INTEGER DEFAULT 1,       -- 1初始/2保本/3跟踪（M2 使用）
    stop_price  REAL,
    status      TEXT DEFAULT 'open',     -- open/closed
    strategy    TEXT DEFAULT 'short',    -- short / ema_trend
    signal_id   TEXT,                    -- 关联信号卡 id
    realized_pnl REAL,                   -- 平仓时写入的已实现盈亏（USDT）
    opened_at   TEXT NOT NULL,
    closed_at   TEXT
);
CREATE TABLE IF NOT EXISTS funding_history (
    symbol  TEXT NOT NULL,
    ts      INTEGER NOT NULL,            -- 毫秒时间戳
    rate    REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS macro_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    event_time TEXT NOT NULL,
    source     TEXT DEFAULT 'manual'     -- builtin/manual
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    action TEXT NOT NULL,                -- enter/exit/scan
    symbol TEXT,
    reason TEXT
);
"""


def _connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表 + 首次初始化默认自选币。"""
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)",
            ("BTC/USDT:USDT", _now()),
        )
        logger.info("SQLite 初始化完成: %s", config.DB_PATH)


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库补列（CREATE TABLE IF NOT EXISTS 不会为已存在表加列）。"""
    pos_cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
    for col, ddl in (
        ("strategy", "ALTER TABLE positions ADD COLUMN strategy TEXT DEFAULT 'short'"),
        ("signal_id", "ALTER TABLE positions ADD COLUMN signal_id TEXT"),
        ("realized_pnl", "ALTER TABLE positions ADD COLUMN realized_pnl REAL"),
    ):
        if col not in pos_cols:
            conn.execute(ddl)
    sig_cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
    if "executed" not in sig_cols:
        conn.execute("ALTER TABLE signals ADD COLUMN executed INTEGER DEFAULT 0")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- 自选币 ----------

def get_watchlist() -> set[str]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT symbol FROM watchlist").fetchall()
    return {r["symbol"] for r in rows}


def add_watch(symbol: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)",
            (symbol, _now()),
        )


def remove_watch(symbol: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))


# ---------- 信号卡 ----------

def save_signal(card) -> None:
    """保存信号卡（card 为 signal.engine.SignalCard 或含 to_dict 的对象）。"""
    d = card.to_dict() if hasattr(card, "to_dict") else card
    with _lock, _connect() as conn:
        # UPSERT：更新信号内容但保留 executed 列（防幂等占位被 REPLACE 覆盖，H3）
        conn.execute(
            "INSERT INTO signals "
            "(id, symbol, direction, confidence, card_json, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "symbol=excluded.symbol, direction=excluded.direction, confidence=excluded.confidence, "
            "card_json=excluded.card_json, status=excluded.status, "
            "created_at=excluded.created_at, expires_at=excluded.expires_at",
            (
                d["id"], d["symbol"], d["direction"], d["confidence"],
                __import__("json").dumps(d, ensure_ascii=False),
                d.get("status", "pending_confirm"),
                d["created_at"], d["expires_at"],
            ),
        )


def mark_signal_executed(signal_id: str) -> bool:
    """原子占位（H3）：把信号标记为已执行；若已被占位/执行返回 False（防并发重复下单）。"""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE signals SET executed = 1 WHERE id = ? AND (executed IS NULL OR executed = 0)",
            (signal_id,),
        )
        return cur.rowcount > 0


def unmark_signal_executed(signal_id: str) -> None:
    """下单失败回滚占位（允许重试）。"""
    with _lock, _connect() as conn:
        conn.execute("UPDATE signals SET executed = 0 WHERE id = ?", (signal_id,))


def has_active_signal(symbol: str, direction: str, strategy_pat: str) -> bool:
    """该 symbol/direction/strategy 是否存在活跃信号（未止损/未过期/未作废）。"""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM signals "
            "WHERE symbol = ? AND direction = ? AND status IN ('pending_confirm','confirmed') "
            "AND card_json LIKE ?",
            (symbol, direction, strategy_pat),
        ).fetchone()
    return int(row["c"]) > 0


def recent_closed_within(symbol: str, direction: str, strategy_pat: str, within_ms: int) -> bool:
    """该 symbol/direction/strategy 最近止损/过期信号是否落在 within_ms 冷却窗口内。"""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) AS m FROM signals "
            "WHERE symbol = ? AND direction = ? AND status IN ('stopped_out','expired') "
            "AND card_json LIKE ?",
            (symbol, direction, strategy_pat),
        ).fetchone()
    m = row["m"]
    return bool(m and (now_ms - int(m)) < within_ms)


def clear_signals() -> int:
    """清空全部信号（历史复盘数据一并清除）。返回删除条数。"""
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM signals")
        return cur.rowcount


def get_active_signals() -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT card_json FROM signals WHERE status != 'expired' ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return [__import__("json").loads(r["card_json"]) for r in rows]


def get_signals(limit: int = 50, symbol: str | None = None, status: str | None = None) -> list[dict]:
    """信号历史列表（按创建时间倒序）。"""
    sql = "SELECT card_json FROM signals WHERE 1=1"
    params: list = []
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _lock, _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [__import__("json").loads(r["card_json"]) for r in rows]


def get_signal(signal_id: str) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT card_json FROM signals WHERE id = ?", (signal_id,)).fetchone()
    return __import__("json").loads(row["card_json"]) if row else None


# ---------- 持仓 ----------

def create_position(symbol: str, direction: str, entry_price: float, qty: float,
                    stop_price: float | None = None, stop_stage: int = 1,
                    strategy: str = "short", signal_id: str | None = None) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO positions (symbol, direction, entry_price, qty, stop_stage, stop_price, status, strategy, signal_id, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (symbol, direction, entry_price, qty, stop_stage, stop_price, strategy, signal_id, _now()),
        )
        return int(cur.lastrowid)


def get_positions(status: str = "open") -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = ? ORDER BY opened_at DESC", (status,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_position(position_id: int) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    return dict(row) if row else None


def update_position(position_id: int, **fields) -> bool:
    """按字段名白名单更新持仓（防注入）。"""
    allowed = {"direction", "entry_price", "qty", "stop_stage", "stop_price", "status",
               "strategy", "signal_id", "realized_pnl", "closed_at"}
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return False
    sets = ", ".join(f"{k} = ?" for k in cols)
    with _lock, _connect() as conn:
        cur = conn.execute(f"UPDATE positions SET {sets} WHERE id = ?",
                           (*cols.values(), position_id))
        return cur.rowcount > 0


def close_position(position_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE positions SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'open'",
            (_now(), position_id),
        )
        return cur.rowcount > 0


def count_positions_opened_today() -> int:
    """当日开仓次数（按 opened_at 的 UTC 日期）。"""
    prefix = _now()[:10]
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM positions WHERE opened_at LIKE ?", (prefix + "%",)
        ).fetchone()
    return int(row["c"])


def sum_realized_pnl_today() -> float:
    """当日已实现盈亏合计（按 closed_at 的 UTC 日期，平仓时写入 realized_pnl）。"""
    prefix = _now()[:10]
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS s FROM positions WHERE closed_at LIKE ?",
            (prefix + "%",),
        ).fetchone()
    return float(row["s"])


# ---------- 宏观日历 ----------

def get_macro_events() -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, event_time, source FROM macro_events ORDER BY event_time"
        ).fetchall()
    return [dict(r) for r in rows]


def add_macro_event(title: str, event_time: str, source: str = "manual") -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO macro_events (title, event_time, source) VALUES (?, ?, ?)",
            (title, event_time, source),
        )


def remove_macro_event(event_id: int) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM macro_events WHERE id = ?", (event_id,))


# ---------- 费率历史（ROC 计算） ----------

def save_funding_rates(rates: dict[str, float], ts_ms: int) -> None:
    """保存全市场费率快照（symbol → rate）。"""
    with _lock, _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO funding_history (symbol, ts, rate) VALUES (?, ?, ?)",
            [(s, ts_ms, r) for s, r in rates.items()],
        )


def get_funding_history(symbol: str, hours: int = 24) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ts, rate FROM funding_history WHERE symbol = ? "
            "AND ts >= ? ORDER BY ts",
            (symbol, int(datetime.now(timezone.utc).timestamp() * 1000) - hours * 3600 * 1000),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- 扫描日志 ----------

def log_scan(action: str, symbol: str | None, reason: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO scan_log (ts, action, symbol, reason) VALUES (?, ?, ?, ?)",
            (_now(), action, symbol, reason),
        )


def get_scan_logs(limit: int = 100) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scan_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- 设置 ----------

def get_setting(key: str, default: str | None = None) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
