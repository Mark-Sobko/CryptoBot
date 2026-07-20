import itertools
import logging
from typing import Any, Dict, List, Optional


class PaperBybitSession:
    """
    Local Bybit V5-shaped session for dry-run lifecycle checks.
    It never sends network requests and only implements methods used by
    TradeExecutor, TPManager, InstrumentCache and exchange synchronization.
    """

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        last_price: float = 100.0,
        balance: float = 1000.0,
    ):
        self.symbol = str(symbol).upper()
        self.last_price = float(last_price)
        self.balance = float(balance)
        self.available_balance = float(balance)
        self._order_seq = itertools.count(1)
        self.open_orders: List[Dict[str, Any]] = []
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_pnl: List[Dict[str, Any]] = []

    @staticmethod
    def _ok(result: Dict[str, Any]) -> Dict[str, Any]:
        return {"retCode": 0, "retMsg": "OK", "result": result}

    def _next_order_id(self, prefix: str = "paper") -> str:
        return f"{prefix}-{next(self._order_seq)}"

    def get_wallet_balance(self, **kwargs) -> Dict[str, Any]:
        return self._ok(
            {
                "list": [
                    {
                        "totalEquity": str(self.balance),
                        "totalAvailableBalance": str(self.available_balance),
                        "walletBalance": str(self.balance),
                    }
                ]
            }
        )

    def get_tickers(self, **kwargs) -> Dict[str, Any]:
        symbol = kwargs.get("symbol", self.symbol)
        return self._ok({"list": [{"symbol": symbol, "lastPrice": str(self.last_price)}]})

    def get_instruments_info(self, **kwargs) -> Dict[str, Any]:
        symbol = kwargs.get("symbol", self.symbol)
        return self._ok(
            {
                "list": [
                    {
                        "symbol": symbol,
                        "priceFilter": {"tickSize": "0.1"},
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.001",
                            "maxOrderQty": "1000000",
                            "minNotionalValue": "5",
                        },
                    }
                ]
            }
        )

    def get_open_orders(self, **kwargs) -> Dict[str, Any]:
        symbol = kwargs.get("symbol")
        orders = self.open_orders
        if symbol:
            orders = [order for order in orders if order.get("symbol") == symbol]
        return self._ok({"list": [dict(order) for order in orders]})

    def get_positions(self, **kwargs) -> Dict[str, Any]:
        symbol = kwargs.get("symbol")
        positions = list(self.positions.values())
        if symbol:
            positions = [pos for pos in positions if pos.get("symbol") == symbol]
        return self._ok({"list": [dict(pos) for pos in positions]})

    def get_closed_pnl(self, **kwargs) -> Dict[str, Any]:
        symbol = kwargs.get("symbol")
        items = self.closed_pnl
        if symbol:
            items = [item for item in items if item.get("symbol") == symbol]
        return self._ok({"list": [dict(item) for item in items[: int(kwargs.get("limit", 50))]]})

    def place_order(self, **kwargs) -> Dict[str, Any]:
        order_id = self._next_order_id("paper-order")
        qty = str(kwargs["qty"])
        order = {
            "orderId": order_id,
            "orderLinkId": kwargs.get("orderLinkId", ""),
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "orderType": kwargs.get("orderType", "Market"),
            "orderStatus": "New",
            "qty": qty,
            "cumExecQty": "0",
            "leavesQty": qty,
            "price": str(kwargs.get("price", self.last_price)),
            "stopLoss": str(kwargs.get("stopLoss", "")),
            "takeProfit": str(kwargs.get("takeProfit", "")),
            "reduceOnly": bool(kwargs.get("reduceOnly", False)),
            "closeOnTrigger": bool(kwargs.get("closeOnTrigger", False)),
            "positionIdx": int(kwargs.get("positionIdx", 0) or 0),
            "timeInForce": kwargs.get("timeInForce", ""),
        }
        self.open_orders.append(order)

        if str(order["orderType"]).upper() == "MARKET" and not order["reduceOnly"]:
            self.fill_order(order_id)

        return self._ok({"orderId": order_id, "orderLinkId": order["orderLinkId"]})

    def set_trading_stop(self, **kwargs) -> Dict[str, Any]:
        symbol = kwargs.get("symbol")
        if str(kwargs.get("takeProfit", "")) == "0" and symbol:
            self.open_orders = [
                order
                for order in self.open_orders
                if not (
                    order.get("symbol") == symbol
                    and order.get("reduceOnly")
                    and order.get("closeOnTrigger")
                )
            ]
        return self._ok({})

    def cancel_order(self, **kwargs) -> Dict[str, Any]:
        order_id = str(kwargs.get("orderId", ""))
        self.open_orders = [order for order in self.open_orders if order.get("orderId") != order_id]
        return self._ok({"orderId": order_id})

    def _upsert_position_from_order(
        self,
        order: Dict[str, Any],
        fill_qty: float,
    ) -> Optional[Dict[str, Any]]:
        fill_qty = float(fill_qty)
        if fill_qty <= 0:
            return None

        symbol = str(order["symbol"])
        entry_price = float(order.get("price", self.last_price) or self.last_price)
        side = str(order.get("side", "Buy"))
        stop_loss = float(order.get("stopLoss", 0.0) or 0.0)
        position_idx = int(order.get("positionIdx", 0) or (1 if side == "Buy" else 2))

        existing = self.positions.get(symbol)
        if existing:
            old_size = float(existing.get("size", 0.0) or 0.0)
            old_entry = float(existing.get("entryPrice", entry_price) or entry_price)
            new_size = old_size + fill_qty
            avg_entry = ((old_entry * old_size) + (entry_price * fill_qty)) / new_size
            existing.update(
                {
                    "size": str(new_size),
                    "entryPrice": str(avg_entry),
                    "markPrice": str(self.last_price),
                    "stopLoss": str(stop_loss or float(existing.get("stopLoss", 0.0) or 0.0)),
                    "positionIdx": position_idx,
                }
            )
            return existing

        position = {
            "symbol": symbol,
            "side": side,
            "size": str(fill_qty),
            "entryPrice": str(entry_price),
            "markPrice": str(self.last_price),
            "stopLoss": str(stop_loss),
            "positionIdx": position_idx,
            "unrealisedPnl": "0",
            "leverage": "1",
        }
        self.positions[symbol] = position
        return position

    def partially_fill_order(self, order_id: str, fill_qty: float) -> Optional[Dict[str, Any]]:
        order = next((item for item in self.open_orders if item.get("orderId") == order_id), None)
        if not order or order.get("reduceOnly"):
            return None

        total_qty = float(order.get("qty", 0.0) or 0.0)
        already_filled = float(order.get("cumExecQty", 0.0) or 0.0)
        remaining_before = max(total_qty - already_filled, 0.0)
        actual_fill = min(float(fill_qty), remaining_before)
        if actual_fill <= 0:
            return None

        position = self._upsert_position_from_order(order, actual_fill)
        if position is None:
            return None

        filled_now = already_filled + actual_fill
        leaves_qty = max(total_qty - filled_now, 0.0)

        if leaves_qty > 0:
            order["orderStatus"] = "PartiallyFilled"
            order["cumExecQty"] = str(filled_now)
            order["leavesQty"] = str(leaves_qty)
        else:
            self.open_orders = [item for item in self.open_orders if item.get("orderId") != order_id]

        order_side = str(order.get("side", "Buy"))
        take_profit = str(order.get("takeProfit", "") or "")
        if take_profit:
            tp_side = "Sell" if order_side == "Buy" else "Buy"
            symbol = str(order["symbol"])
            position_idx = int(order.get("positionIdx", 0) or (1 if order_side == "Buy" else 2))
            self.open_orders.append(
                {
                    "orderId": self._next_order_id("paper-system-tp"),
                    "symbol": symbol,
                    "side": tp_side,
                    "orderType": "Limit",
                    "orderStatus": "New",
                    "qty": str(actual_fill),
                    "cumExecQty": "0",
                    "leavesQty": str(actual_fill),
                    "price": take_profit,
                    "reduceOnly": True,
                    "closeOnTrigger": True,
                    "positionIdx": position_idx,
                    "timeInForce": "GTC",
                }
            )

        return position

    def fill_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        order = next((item for item in self.open_orders if item.get("orderId") == order_id), None)
        if not order:
            return None

        total_qty = float(order.get("qty", 0.0) or 0.0)
        already_filled = float(order.get("cumExecQty", 0.0) or 0.0)
        return self.partially_fill_order(order_id, max(total_qty - already_filled, 0.0))

    def close_position(self, symbol: str, exit_price: float) -> Optional[Dict[str, Any]]:
        symbol = str(symbol).upper()
        position = self.positions.pop(symbol, None)
        if not position:
            return None

        qty = float(position.get("size", 0.0) or 0.0)
        entry_price = float(position.get("entryPrice", 0.0) or 0.0)
        exit_price = float(exit_price)
        side = str(position.get("side", "Buy"))

        pnl_usd = (exit_price - entry_price) * qty if side == "Buy" else (entry_price - exit_price) * qty
        self.balance += pnl_usd
        self.available_balance += pnl_usd
        self.last_price = exit_price

        self.open_orders = [order for order in self.open_orders if order.get("symbol") != symbol]

        item = {
            "symbol": symbol,
            "closedPnl": str(round(pnl_usd, 8)),
            "avgExitPrice": str(exit_price),
            "avgEntryPrice": str(entry_price),
            "qty": str(qty),
        }
        self.closed_pnl.insert(0, item)
        return item


class PaperExchangeManager:
    CATEGORY = "linear"
    SETTLE_COIN = "USDT"

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        last_price: float = 100.0,
        balance: float = 1000.0,
    ):
        self.logger = logging.getLogger("SMC_BOT.PaperExchange")
        self.session = PaperBybitSession(symbol=symbol, last_price=last_price, balance=balance)
        self.retry_attempts = 1

    @staticmethod
    def _normalize_position_side(side: str) -> str:
        side_upper = str(side).upper().strip()
        if side_upper in ("BUY", "LONG"):
            return "LONG"
        if side_upper in ("SELL", "SHORT"):
            return "SHORT"
        return side_upper

    @staticmethod
    def _is_success(res: Dict[str, Any]) -> bool:
        return isinstance(res, dict) and res.get("retCode") == 0

    def _request_with_retry(self, func, *args, **kwargs) -> Optional[Dict[str, Any]]:
        try:
            res = func(*args, **kwargs)
            return res if self._is_success(res) else None
        except Exception as e:
            self.logger.error(f"🛑 [PAPER API ERROR] {e}")
            return None

    def get_total_balance(self) -> float:
        return float(self.session.balance)

    def get_available_balance(self) -> float:
        return float(self.session.available_balance)

    def get_active_positions(self) -> List[Dict[str, Any]]:
        res = self.session.get_positions(category=self.CATEGORY, settleCoin=self.SETTLE_COIN)
        positions: List[Dict[str, Any]] = []

        for pos in res.get("result", {}).get("list", []):
            size = float(pos.get("size", 0.0) or 0.0)
            if size <= 0:
                continue

            entry_price = float(pos.get("entryPrice", 0.0) or 0.0)
            mark_price = float(pos.get("markPrice", entry_price) or entry_price)
            stop_loss = float(pos.get("stopLoss", 0.0) or 0.0)
            side = str(pos.get("side", ""))

            positions.append(
                {
                    "symbol": pos.get("symbol"),
                    "side": side,
                    "size": size,
                    "entry_price": entry_price,
                    "entryPrice": entry_price,
                    "mark_price": mark_price,
                    "markPrice": mark_price,
                    "stop_loss": stop_loss,
                    "stopLoss": stop_loss,
                    "positionIdx": int(pos.get("positionIdx", 0) or 0),
                    "unrealisedPnl": float(pos.get("unrealisedPnl", 0.0) or 0.0),
                    "leverage": float(pos.get("leverage", 0.0) or 0.0),
                }
            )

        return positions

    def get_pending_entry_orders(self, symbol: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        res = self.session.get_open_orders(category=self.CATEGORY, symbol=symbol)
        if not self._is_success(res):
            return None

        pending_orders: List[Dict[str, Any]] = []
        for order in res.get("result", {}).get("list", []):
            order_type = str(order.get("orderType", "")).upper()
            order_status = str(order.get("orderStatus", "")).upper()
            reduce_only = str(order.get("reduceOnly", "")).lower() == "true"
            close_on_trigger = str(order.get("closeOnTrigger", "")).lower() == "true"

            if order_type == "LIMIT" and order_status in {"NEW", "PARTIALLYFILLED", "UNTRIGGERED"}:
                if not reduce_only and not close_on_trigger:
                    pending_orders.append(order)

        return pending_orders

    def can_open_new_trade(self, max_trades: int) -> bool:
        pending_orders = self.get_pending_entry_orders()
        if pending_orders is None:
            return False
        return len(self.get_active_positions()) + len(pending_orders) < int(max_trades)

    def get_latest_closed_pnl(self, symbol: str) -> Optional[Dict[str, float]]:
        res = self.session.get_closed_pnl(category=self.CATEGORY, symbol=symbol, limit=1)
        items = res.get("result", {}).get("list", [])
        if not items:
            return None

        item = items[0]
        pnl_usd = float(item.get("closedPnl", 0.0) or 0.0)
        exit_price = float(item.get("avgExitPrice", item.get("exitPrice", 0.0)) or 0.0)
        qty = float(item.get("qty", 0.0) or 0.0)
        entry_price = float(item.get("avgEntryPrice", item.get("entryPrice", 0.0)) or 0.0)
        pnl_pct = (pnl_usd / (entry_price * qty)) * 100.0 if qty > 0 and entry_price > 0 else 0.0

        return {
            "exit_price": exit_price,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
        }

    def sync_db_with_exchange(self, db_instance) -> None:
        active_on_exchange = self.get_active_positions()
        active_keys_on_exchange = set()

        for pos in active_on_exchange:
            symbol = str(pos.get("symbol", ""))
            side = self._normalize_position_side(str(pos.get("side", "")))
            active_keys_on_exchange.add((symbol, side))
            db_instance.mark_trade_open(
                symbol=symbol,
                side=side,
                entry_price=float(pos.get("entry_price", pos.get("entryPrice", 0.0)) or 0.0),
                qty=float(pos.get("size", 0.0) or 0.0),
                stop_loss=float(pos.get("stop_loss", pos.get("stopLoss", 0.0)) or 0.0),
            )

        for trade in db_instance.get_open_positions():
            symbol = str(trade.get("symbol", ""))
            side = self._normalize_position_side(str(trade.get("side", "")))
            if (symbol, side) in active_keys_on_exchange:
                continue

            closed_pnl = self.get_latest_closed_pnl(symbol)
            close_status = "CLOSED" if closed_pnl else "CLOSED_UNVERIFIED"
            db_instance.close_trade(
                symbol=symbol,
                exit_price=closed_pnl["exit_price"] if closed_pnl else 0.0,
                pnl_usd=closed_pnl["pnl_usd"] if closed_pnl else 0.0,
                pnl_pct=closed_pnl["pnl_pct"] if closed_pnl else 0.0,
                trade_id=trade.get("id"),
                status=close_status,
            )

        for trade in getattr(db_instance, "get_pending_orders", lambda: [])():
            symbol = str(trade.get("symbol", ""))
            side = self._normalize_position_side(str(trade.get("side", "")))
            if (symbol, side) in active_keys_on_exchange:
                continue

            order_id = str(trade.get("order_id", "") or "")
            if not order_id:
                continue

            open_orders = self.session.get_open_orders(category=self.CATEGORY, symbol=symbol)
            still_pending = any(
                str(order.get("orderId", "")) == order_id
                for order in open_orders.get("result", {}).get("list", [])
            )

            if not still_pending:
                db_instance.mark_trade_cancelled(
                    symbol=symbol,
                    order_id=order_id,
                    trade_id=trade.get("id"),
                )
