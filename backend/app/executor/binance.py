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
        self._dual_mode: Optional[bool] = None  # 账户双向持仓模式缓存（Hedge Mode）

    # ---------- 持仓模式（双向/单向） ----------

    def _dual_position_mode(self) -> bool:
        """账户是否双向持仓模式（Hedge Mode）：双向下单必须带 positionSide。

        结果缓存（模式基本不变）；查询失败按单向处理（避免阻断下单）。
        """
        if not self.configured or self.dry_run:
            return False
        if self._dual_mode is None:
            try:
                res = self._client().fapiPrivateGetPositionSideDual()
                self._dual_mode = str(res.get("dualSidePosition", "false")).lower() == "true"
                logger.info("币安持仓模式: %s", "双向(hedge)" if self._dual_mode else "单向")
            except Exception as exc:  # noqa: BLE001
                logger.warning("持仓模式查询失败（按单向处理）: %s", exc)
                self._dual_mode = False
        return self._dual_mode

    def _order_params(self, side: str, reduce_only: bool = False, position_side: str | None = None) -> dict:
        """下单附加参数：双向模式下带 positionSide；平仓带 reduceOnly。

        position_side: 该订单作用的持仓侧（LONG/SHORT）。开仓不传时按订单方向
        （buy→LONG / sell→SHORT）；**平仓必须由调用方传持仓侧**（平多=sell 单但
        positionSide=LONG，否则 -4061）。
        """
        params = {}
        if reduce_only and not self._dual_position_mode():
            # 单向模式：平仓必须带 reduceOnly 防反向开仓；
            # 双向模式：positionSide 已限定持仓侧，币安不接受冗余 reduceOnly（-1106）。
            params["reduceOnly"] = True
        if self._dual_position_mode():
            params["positionSide"] = position_side or ("LONG" if side == "buy" else "SHORT")
        return params

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
        """U 本位持仓列表（过滤空仓）。

        双向持仓模式下 ccxt 的 contracts 可能为绝对值，方向以 `side` 字段为准。
        """
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
                    "side": p.get("side") or ("long" if contracts > 0 else "short"),
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

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """设置币安杠杆（限制 1 ~ BINANCE_MAX_LEVERAGE，不允许超上限）。"""
        lev = max(1, min(int(leverage), config.BINANCE_MAX_LEVERAGE))
        if self.dry_run:
            logger.info("[DRY-RUN] set_leverage %s %sx", symbol, lev)
            return {"ok": True, "dry_run": True, "leverage": lev}
        try:
            self._client().set_leverage(lev, symbol)
            return {"ok": True, "leverage": lev}
        except Exception as exc:  # noqa: BLE001
            logger.error("设置杠杆失败 %s %sx: %s", symbol, lev, exc)
            return {"ok": False, "error": f"设置杠杆失败: {exc}"}

    def create_order(self, symbol: str, side: str, amount: float,
                     order_type: str = "market", price: Optional[float] = None,
                     reduce_only: bool = False, position_side: Optional[str] = None) -> dict:
        """创建订单。side: buy/sell；amount: 合约数量。

        reduce_only=True 时下单带 reduceOnly（平仓单，防止无持仓时误开反向新仓，H5）。
        position_side: 双向持仓模式下平仓单需传持仓侧（LONG/SHORT），开仓可不传。
        dry_run=True 时仅模拟记录并返回模拟订单。
        """
        if amount <= 0:
            return {"ok": False, "error": "数量必须 > 0"}
        if not self.configured:
            return {"ok": False, "error": "未配置 BINANCE_API_KEY/SECRET"}
        if self.dry_run:
            logger.info("[DRY-RUN] %s %s %s amount=%s price=%s reduce_only=%s",
                        order_type, side, symbol, amount, price, reduce_only)
            return {
                "ok": True, "dry_run": True, "id": f"dry_{int(time.time() * 1000)}",
                "symbol": symbol, "side": side, "type": order_type,
                "amount": amount, "price": price, "reduce_only": reduce_only, "status": "closed",
            }
        try:
            ex = self._client()
            kwargs = {}
            if order_type == "limit" and price is not None:
                kwargs["price"] = price
            kwargs["params"] = self._order_params(side, reduce_only, position_side)  # 双向模式带 positionSide；平仓带 reduceOnly
            order = ex.create_order(symbol, order_type, side, amount, **kwargs)
            return {
                "ok": True, "dry_run": False,
                "id": order.get("id"), "symbol": order.get("symbol"),
                "side": order.get("side"), "type": order.get("type"),
                "amount": order.get("amount"), "price": order.get("price"),
                "reduce_only": reduce_only, "status": order.get("status"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("下单失败 %s %s %s: %s", symbol, side, amount, exc)
            return {"ok": False, "error": str(exc)}

    def create_stop_loss_order(self, symbol: str, side: str, amount: float, stop_price: float,
                               position_side: Optional[str] = None) -> dict:
        """交易所侧 STOP_MARKET 止损单（H7，进程外保护）。

        side: 平仓方向（多头→sell / 空头→buy）；stop_price: 触发止损价；
        position_side: 双向模式下传持仓侧（多头→LONG / 空头→SHORT）。
        """
        if not self.configured:
            return {"ok": False, "error": "未配置 BINANCE_API_KEY/SECRET"}
        if self.dry_run:
            logger.info("[DRY-RUN] STOP_MARKET %s %s amount=%s stop=%s", side, symbol, amount, stop_price)
            return {"ok": True, "dry_run": True, "id": f"dry_stop_{int(time.time() * 1000)}",
                    "symbol": symbol, "side": side, "stop_price": stop_price, "status": "open"}
        try:
            ex = self._client()
            order = ex.create_order(
                symbol, "STOP_MARKET", side, amount,
                params={"stopPrice": stop_price, **self._order_params(side, True, position_side)},
            )
            return {"ok": True, "dry_run": False, "id": order.get("id"),
                    "symbol": symbol, "side": side, "stop_price": stop_price,
                    "status": order.get("status")}
        except Exception as exc:  # noqa: BLE001
            logger.error("挂止损单失败 %s %s stop=%s: %s", symbol, side, stop_price, exc)
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

    # ---------- 数量精度 / 最小下单量（M5） ----------

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """按币种数量精度截断（ccxt amount_to_precision，LOT_SIZE step 对齐）。失败回退 6 位。"""
        try:
            ex = self._client()
            return float(ex.amount_to_precision(symbol, amount))
        except Exception as exc:  # noqa: BLE001
            logger.warning("数量精度截断失败 %s: %s", symbol, exc)
            return round(amount, 6)

    def min_amount(self, symbol: str) -> float:
        """币种最小下单量（limits.amount.min）。"""
        try:
            market = self._client().market(symbol)
            return float(market.get("limits", {}).get("amount", {}).get("min") or 0)
        except Exception:  # noqa: BLE001
            return 0.0
