#!/usr/bin/env python3
"""Run a tiny Bybit demo/testnet trading lifecycle.

The script is intentionally separate from main.py so it never starts the
strategy scanner. It fails closed unless BYBIT_DEMO or BYBIT_TESTNET is true.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable

from pybit.unified_trading import HTTP

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config


CATEGORY = "linear"
SETTLE_COIN = "USDT"


class LifecycleError(RuntimeError):
    pass


def decimal_from(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        value = default
    return Decimal(str(value))


def decimal_to_str(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")


def round_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        return value
    units = value / step
    mode = ROUND_UP if rounding == "up" else ROUND_DOWN
    return units.to_integral_value(rounding=mode) * step


def api_call(func: Callable[..., dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    try:
        res = func(**kwargs)
    except Exception as exc:  # pybit raises for some non-zero retCodes.
        raise LifecycleError(f"{func.__name__} exception: {exc}") from exc

    if not isinstance(res, dict) or res.get("retCode") != 0:
        raise LifecycleError(
            f"{func.__name__} failed: retCode={res.get('retCode') if isinstance(res, dict) else None} "
            f"retMsg={res.get('retMsg') if isinstance(res, dict) else res}"
        )

    return res


def try_place_order(session: HTTP, **kwargs: Any) -> tuple[bool, dict[str, Any] | str]:
    try:
        res = session.place_order(**kwargs)
    except Exception as exc:
        return False, str(exc)

    if not isinstance(res, dict) or res.get("retCode") != 0:
        return False, res

    return True, res


def get_instrument(session: HTTP, symbol: str) -> dict[str, Decimal]:
    res = api_call(session.get_instruments_info, category=CATEGORY, symbol=symbol)
    items = res.get("result", {}).get("list", [])
    if not items:
        raise LifecycleError(f"{symbol}: instrument not found")

    item = items[0]
    price_filter = item["priceFilter"]
    lot_filter = item["lotSizeFilter"]
    return {
        "tick_size": decimal_from(price_filter["tickSize"]),
        "qty_step": decimal_from(lot_filter["qtyStep"]),
        "min_qty": decimal_from(lot_filter["minOrderQty"]),
        "min_notional": decimal_from(lot_filter.get("minNotionalValue", "0")),
    }


def get_last_price(session: HTTP, symbol: str) -> Decimal:
    res = api_call(session.get_tickers, category=CATEGORY, symbol=symbol)
    items = res.get("result", {}).get("list", [])
    if not items:
        raise LifecycleError(f"{symbol}: ticker not found")

    price = decimal_from(items[0].get("lastPrice"))
    if price <= 0:
        raise LifecycleError(f"{symbol}: invalid last price")
    return price


def get_open_orders(session: HTTP, symbol: str) -> list[dict[str, Any]]:
    res = api_call(session.get_open_orders, category=CATEGORY, symbol=symbol)
    return list(res.get("result", {}).get("list", []))


def get_positions(session: HTTP, symbol: str) -> list[dict[str, Any]]:
    res = api_call(session.get_positions, category=CATEGORY, symbol=symbol)
    positions = []
    for item in res.get("result", {}).get("list", []):
        if decimal_from(item.get("size")) > 0:
            positions.append(item)
    return positions


def wait_for_position(
    session: HTTP,
    symbol: str,
    *,
    want_open: bool,
    timeout_s: float,
) -> list[dict[str, Any]]:
    deadline = time.time() + timeout_s
    last_positions: list[dict[str, Any]] = []

    while time.time() < deadline:
        last_positions = get_positions(session, symbol)
        if bool(last_positions) == want_open:
            return last_positions
        time.sleep(0.5)

    state = "open" if want_open else "flat"
    raise LifecycleError(f"{symbol}: timed out waiting for position state {state}; last={last_positions}")


def cancel_prefix_orders(session: HTTP, symbol: str, prefix: str) -> int:
    count = 0
    for order in get_open_orders(session, symbol):
        link_id = str(order.get("orderLinkId", "") or "")
        order_id = str(order.get("orderId", "") or "")
        if not order_id or not link_id.startswith(prefix):
            continue

        api_call(session.cancel_order, category=CATEGORY, symbol=symbol, orderId=order_id)
        count += 1
        time.sleep(0.2)
    return count


def close_open_positions(session: HTTP, symbol: str) -> int:
    closed = 0
    for pos in get_positions(session, symbol):
        size = decimal_from(pos.get("size"))
        if size <= 0:
            continue

        side = str(pos.get("side", "Buy"))
        close_side = "Sell" if side == "Buy" else "Buy"
        position_idx = int(pos.get("positionIdx", 0) or 0)

        api_call(
            session.place_order,
            category=CATEGORY,
            symbol=symbol,
            side=close_side,
            orderType="Market",
            qty=decimal_to_str(size),
            reduceOnly=True,
            timeInForce="IOC",
            positionIdx=position_idx,
        )
        closed += 1
        time.sleep(0.5)
    return closed


def choose_qty(
    last_price: Decimal,
    min_order_price: Decimal,
    instrument: dict[str, Decimal],
    max_notional: Decimal,
) -> Decimal:
    min_qty = instrument["min_qty"]
    qty_step = instrument["qty_step"]
    min_notional = instrument["min_notional"]

    qty = min_qty
    notional_basis = min(last_price, min_order_price)
    if min_notional > 0 and qty * notional_basis < min_notional:
        qty = (min_notional / notional_basis) * Decimal("1.05")

    qty = round_step(qty, qty_step, "up")
    notional = qty * last_price
    if notional > max_notional:
        raise LifecycleError(
            f"min lifecycle notional {decimal_to_str(notional)} exceeds max "
            f"{decimal_to_str(max_notional)}; choose another symbol or raise --max-notional"
        )

    return qty


def place_far_limit_with_mode_fallback(
    session: HTTP,
    *,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    order_link_id: str,
) -> tuple[int, str]:
    base = {
        "category": CATEGORY,
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Limit",
        "qty": decimal_to_str(qty),
        "price": decimal_to_str(price),
        "timeInForce": "PostOnly",
        "orderLinkId": order_link_id,
    }

    attempts: list[tuple[int, Any]] = []
    for position_idx in (0, 1):
        ok, result = try_place_order(session, **base, positionIdx=position_idx)
        attempts.append((position_idx, result))
        if ok:
            order_id = str(result.get("result", {}).get("orderId", ""))
            if not order_id:
                raise LifecycleError("limit order placed but orderId missing")
            return position_idx, order_id

    raise LifecycleError(f"failed to place limit order in one-way or hedge mode: {attempts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XRPUSDT")
    parser.add_argument("--max-notional", type=Decimal, default=Decimal("25"))
    parser.add_argument("--wait", type=float, default=12.0)
    args = parser.parse_args()

    if not (config.BYBIT_DEMO or config.BYBIT_TESTNET):
        raise SystemExit("SAFEGUARD: BYBIT_DEMO and BYBIT_TESTNET are both false")

    session = HTTP(
        api_key=config.API_KEY,
        api_secret=config.API_SECRET,
        demo=config.BYBIT_DEMO,
        testnet=config.BYBIT_TESTNET,
        recv_window=20000,
        timeout=int(config.RISK_MANAGEMENT.get("global", {}).get("request_timeout", 30)),
    )

    symbol = args.symbol.upper()
    prefix = f"cdx{int(time.time())}"
    opened_position = False
    summary: dict[str, Any] = {
        "status": "STARTED",
        "environment": {"demo": config.BYBIT_DEMO, "testnet": config.BYBIT_TESTNET},
        "symbol": symbol,
        "prefix": prefix,
        "steps": [],
    }

    try:
        if get_positions(session, symbol):
            raise LifecycleError(f"{symbol}: active position exists before lifecycle; aborting")
        if get_open_orders(session, symbol):
            raise LifecycleError(f"{symbol}: open orders exist before lifecycle; aborting")

        instrument = get_instrument(session, symbol)
        last_price = get_last_price(session, symbol)
        tick_size = instrument["tick_size"]

        far_limit_price = round_step(last_price * Decimal("0.80"), tick_size, "down")
        qty = choose_qty(last_price, far_limit_price, instrument, args.max_notional)
        position_idx, pending_order_id = place_far_limit_with_mode_fallback(
            session,
            symbol=symbol,
            qty=qty,
            price=far_limit_price,
            order_link_id=f"{prefix}-limit",
        )
        summary["steps"].append(
            {
                "name": "limit_create",
                "order_id": pending_order_id,
                "position_idx": position_idx,
                "qty": decimal_to_str(qty),
                "price": decimal_to_str(far_limit_price),
            }
        )

        open_ids = {str(order.get("orderId", "")) for order in get_open_orders(session, symbol)}
        if pending_order_id not in open_ids:
            raise LifecycleError("created limit order was not visible in open orders")

        api_call(session.cancel_order, category=CATEGORY, symbol=symbol, orderId=pending_order_id)
        summary["steps"].append({"name": "limit_cancel", "order_id": pending_order_id})
        time.sleep(0.5)

        open_ids_after_cancel = {str(order.get("orderId", "")) for order in get_open_orders(session, symbol)}
        if pending_order_id in open_ids_after_cancel:
            raise LifecycleError("cancelled limit order is still visible in open orders")

        open_link = f"{prefix}-open"
        api_call(
            session.place_order,
            category=CATEGORY,
            symbol=symbol,
            side="Buy",
            orderType="Market",
            qty=decimal_to_str(qty),
            timeInForce="IOC",
            orderLinkId=open_link,
            positionIdx=position_idx,
        )
        opened_position = True
        positions = wait_for_position(session, symbol, want_open=True, timeout_s=args.wait)
        position = positions[0]
        entry_price = decimal_from(position.get("avgPrice") or position.get("entryPrice") or last_price)
        position_idx = int(position.get("positionIdx", position_idx) or position_idx)
        open_qty = decimal_from(position.get("size"), decimal_to_str(qty))
        summary["steps"].append(
            {
                "name": "market_open",
                "position_idx": position_idx,
                "qty": decimal_to_str(open_qty),
                "entry_price": decimal_to_str(entry_price),
            }
        )

        tp_price = round_step(entry_price * Decimal("1.20"), tick_size, "up")
        tp_res = api_call(
            session.place_order,
            category=CATEGORY,
            symbol=symbol,
            side="Sell",
            orderType="Limit",
            qty=decimal_to_str(open_qty),
            price=decimal_to_str(tp_price),
            reduceOnly=True,
            closeOnTrigger=False,
            timeInForce="PostOnly",
            orderLinkId=f"{prefix}-tp",
            positionIdx=position_idx,
        )
        tp_order_id = str(tp_res.get("result", {}).get("orderId", ""))
        summary["steps"].append({"name": "reduce_only_tp_create", "order_id": tp_order_id})

        open_orders = get_open_orders(session, symbol)
        tp_order = next((order for order in open_orders if str(order.get("orderId", "")) == tp_order_id), None)
        if not tp_order or str(tp_order.get("reduceOnly", "")).lower() != "true":
            raise LifecycleError("reduce-only TP was not visible as reduceOnly=true")

        sl_price = round_step(entry_price * Decimal("0.80"), tick_size, "down")
        api_call(
            session.set_trading_stop,
            category=CATEGORY,
            symbol=symbol,
            stopLoss=decimal_to_str(sl_price),
            slTriggerBy="LastPrice",
            positionIdx=position_idx,
        )
        summary["steps"].append({"name": "stop_loss_set", "price": decimal_to_str(sl_price)})

        api_call(session.cancel_order, category=CATEGORY, symbol=symbol, orderId=tp_order_id)
        summary["steps"].append({"name": "reduce_only_tp_cancel", "order_id": tp_order_id})
        time.sleep(0.5)

        api_call(
            session.set_trading_stop,
            category=CATEGORY,
            symbol=symbol,
            stopLoss="0",
            positionIdx=position_idx,
        )
        summary["steps"].append({"name": "stop_loss_clear"})

        api_call(
            session.place_order,
            category=CATEGORY,
            symbol=symbol,
            side="Sell",
            orderType="Market",
            qty=decimal_to_str(open_qty),
            reduceOnly=True,
            timeInForce="IOC",
            orderLinkId=f"{prefix}-close",
            positionIdx=position_idx,
        )
        opened_position = False
        wait_for_position(session, symbol, want_open=False, timeout_s=args.wait)
        summary["steps"].append({"name": "market_close_reduce_only"})

        if any(str(order.get("orderLinkId", "")).startswith(prefix) for order in get_open_orders(session, symbol)):
            raise LifecycleError("prefix orders remain open after lifecycle")

        summary["status"] = "OK"
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    finally:
        cleanup = {"prefix_orders_cancelled": 0, "positions_closed": 0}
        try:
            cleanup["prefix_orders_cancelled"] = cancel_prefix_orders(session, symbol, prefix)
        except Exception as exc:
            cleanup["prefix_order_cleanup_error"] = str(exc)

        if opened_position:
            try:
                cleanup["positions_closed"] = close_open_positions(session, symbol)
            except Exception as exc:
                cleanup["position_cleanup_error"] = str(exc)

        if cleanup["prefix_orders_cancelled"] or cleanup["positions_closed"] or len(cleanup) > 2:
            print(json.dumps({"cleanup": cleanup}, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
