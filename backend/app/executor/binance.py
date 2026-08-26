"""币安 U 本位合约交易执行器（N1 执行层）。

- 配置：`data/settings.json` 的 `BINANCE_API_KEY` / `BINANCE_API_SECRET`（config 启动加载覆盖）。
- **默认纸面模式（dry_run）**：`BINANCE_DRY_RUN=True` 时不真正下单，仅模拟记录；
  确认小额实盘后再置 `False`。
- 安全：单笔金额上限 `BINANCE_MAX_ORDER_USDT`、单日开仓上限 / 亏损熔断由上层风控执行；
  API Key 仅需「合约交易」权限，绝不开提现权限。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .. import config

logger = logging.getLogger(__name__)


class BinanceExecutor:
    """封装 ccxt（binanceusdm）交易接口：余额 / 持仓 / 下单 / 撤单。"""

    def __init__(self) -> None:
        self._client_cache = None
        self.api_key = config.BINANCE_API_KEY or ""
        self.api_secret = config.BINANCE_API_SECRET or ""
        self.dry_run = bool(config.BINANCE_DRY_RUN)

    # ---------- 配置 ----------

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def mode_label(self) -> str:
        return "纸面(dry-run)" if self.dry_run else "实盘"

    # ---------- ccxt 客户端 ----------

    def _client(self):
        if self._client_cache is None:
            import ccxt

            opts = {
                "apiKey": self.api_key,
                "secret": self.api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }
            if config.PROXY:
                opts["proxies"] = {"http": config.PROXY, "https": config.PROXY}
            self._client_cache = ccxt.binanceusdm(opts)
        return self._client_cache

    # ---------- 查询 ----------

    def fetch_balance(self) -> dict:
        """返回 USDT 余额。未配置/异常时返回 {ok:False, error} 而非抛异常。"""
        if not self.configured:
            return {"ok": False, "error": "未配置 BINANCE_API_KEY/SECRET（data/settings.json）"}
        try:
            bal = self._client().fetch_balance()
            usdt = bal.get("USDT") or {}
            return {
                "ok": True,
                "total": usdt.get("total"),
                "free": usdt.get("free"),
                "used": usdt.get("used"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("余额查询失败: %s", exc)
            return {"ok": False, "error": str(exc)}

    def fetch_positions(self) -> list[dict]:
        """U 本位持仓列表（过滤空仓）。"""
        if not self.configured:
            return []
        try:
            out = []
            for p in self._client().fetch_positions():
                contracts = float(p.get("contracts") or 0)
                if abs(contracts) <= 0:
                    continue
                out.append({
                    "symbol": p.get("symbol"),
                    "side": "long" if contracts > 0 else "short",
                    "contracts": abs(contracts),
                    "entry_price": p.get("entryPrice"),
                    "unrealized_pnl": p.get("unrealizedPnl"),
                    "leverage": p.get("leverage"),
                })
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("持仓查询失败: %s", exc)
            return []

    # ---------- 下单 ----------

    def create_order(self, symbol: str, side: str, amount: float,
                     order_type: str = "market", price: Optional[float] = None) -> dict:
        """创建订单。side: buy/sell；amount: 合约数量。

        dry_run=True 时仅模拟记录并返回模拟订单。
        """
        if amount <= 0:
            return {"ok": False, "error": "数量必须 > 0"}
        if not self.configured:
            return {"ok": False, "error": "未配置 BINANCE_API_KEY/SECRET"}
        if self.dry_run:
            logger.info("[DRY-RUN] %s %s %s amount=%s price=%s", order_type, side, symbol, amount, price)
            return {
                "ok": True, "dry_run": True, "id": f"dry_{int(time.time() * 1000)}",
                "symbol": symbol, "side": side, "type": order_type,
                "amount": amount, "price": price, "status": "closed",
            }
        try:
            ex = self._client()
            # 杠杆上限控制（≤ BINANCE_MAX_LEVERAGE，默认 3 倍；失败即拒绝下单，防实际杠杆超限）
            try:
                ex.set_leverage(config.BINANCE_MAX_LEVERAGE, symbol)
            except Exception as exc:  # noqa: BLE001
                logger.error("设置杠杆失败 %s: %s", symbol, exc)
                return {"ok": False, "error": f"设置杠杆失败: {exc}"}
            kwargs = {}
            if order_type == "limit" and price is not None:
                kwargs["price"] = price
            order = ex.create_order(symbol, order_type, side, amount, **kwargs)
            return {
                "ok": True, "dry_run": False,
                "id": order.get("id"), "symbol": order.get("symbol"),
                "side": order.get("side"), "type": order.get("type"),
                "amount": order.get("amount"), "price": order.get("price"),
                "status": order.get("status"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("下单失败 %s %s %s: %s", symbol, side, amount, exc)
            return {"ok": False, "error": str(exc)}

    def cancel_order(self, symbol: str, order_id: str) -> dict:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "id": order_id, "cancelled": True}
        try:
            self._client().cancel_order(order_id, symbol)
            return {"ok": True, "id": order_id, "cancelled": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("撤单失败 %s %s: %s", symbol, order_id, exc)
            return {"ok": False, "error": str(exc)}
