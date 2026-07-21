#!/usr/bin/env python3
"""Run a tiny Bybit demo/testnet trading lifecycle.

The script is intentionally separate from main.py so it never starts the
strategy scanner. It fails closed unless BYBIT_DEMO or BYBIT_TESTNET is true.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable

from pybit.unified_trading import HTTP

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from core.database import TradeDatabase
from core.database_sync import DatabaseSync


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


def try_api_call(func: Callable[..., dict[str, Any]], **kwargs: Any) -> tuple[bool, dict[str, Any] | str]:
    try:
        res = func(**kwargs)
    except Exception as exc:
        return False, str(exc)

    if not isinstance(res, dict) or res.get("retCode") != 0:
        return False, res

    return True, res


def summarize_failure(result: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(result, dict):
        return {
            "retCode": result.get("retCode"),
            "retMsg": str(result.get("retMsg", ""))[:160],
        }

    first_line = str(result).splitlines()[0] if str(result) else ""
    return {"exception": first_line[:220]}


def summarize_place_attempts(attempts: list[tuple[int, Any]]) -> list[dict[str, Any]]:
    return [
        {"position_idx": position_idx, **summarize_failure(result)}
        for position_idx, result in attempts
    ]


def classify_probe_error(error: str) -> str:
    normalized = error.lower()
    if "110126" in normalized or "required agreement" in normalized:
        return "agreement_required"
    if "position idx not match" in normalized:
        return "position_mode_mismatch"
    return "probe_error"


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


def get_raw_positions(session: HTTP, symbol: str) -> list[dict[str, Any]]:
    res = api_call(session.get_positions, category=CATEGORY, symbol=symbol)
    return list(res.get("result", {}).get("list", []))


def position_idx_candidates(session: HTTP, symbol: str, side: str) -> list[int]:
    side = side.capitalize()
    fallback = [1, 0] if side == "Buy" else [2, 0]

    try:
        raw_positions = get_raw_positions(session, symbol)
    except Exception:
        return fallback

    indexes: list[int] = []
    for item in raw_positions:
        position_idx = int(item.get("positionIdx", 0) or 0)
        item_side = str(item.get("side", "") or "").capitalize()

        if position_idx == 0:
            indexes.append(0)
        elif side == "Buy" and position_idx == 1:
            indexes.append(1)
        elif side == "Sell" and position_idx == 2:
            indexes.append(2)
        elif not item_side and position_idx in fallback:
            indexes.append(position_idx)

    for position_idx in fallback:
        indexes.append(position_idx)

    result: list[int] = []
    seen: set[int] = set()
    for position_idx in indexes:
        if position_idx in seen:
            continue
        seen.add(position_idx)
        result.append(position_idx)
    return result


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


def assert_symbol_flat(session: HTTP, symbol: str) -> None:
    positions = get_positions(session, symbol)
    orders = get_open_orders(session, symbol)
    if positions or orders:
        raise LifecycleError(
            f"{symbol}: expected flat state, positions={len(positions)}, open_orders={len(orders)}"
        )


def choose_qty(
    last_price: Decimal,
    min_order_price: Decimal,
    instrument: dict[str, Decimal],
    max_notional: Decimal,
    require_partial_close: bool = True,
) -> Decimal:
    min_qty = instrument["min_qty"]
    qty_step = instrument["qty_step"]
    min_notional = instrument["min_notional"]

    qty = min_qty
    notional_basis = min(last_price, min_order_price)
    if min_notional > 0 and qty * notional_basis < min_notional:
        qty = (min_notional / notional_basis) * Decimal("1.05")

    if require_partial_close and min_notional > 0:
        qty_for_two_valid_halves = (min_notional * Decimal("2.10")) / last_price
        qty = max(qty, qty_for_two_valid_halves)

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
    side: str = "Buy",
    time_in_force: str = "PostOnly",
) -> tuple[int, str]:
    base = {
        "category": CATEGORY,
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "qty": decimal_to_str(qty),
        "price": decimal_to_str(price),
        "timeInForce": time_in_force,
        "orderLinkId": order_link_id,
    }

    attempts: list[tuple[int, Any]] = []
    for position_idx in position_idx_candidates(session, symbol, side):
        ok, result = try_place_order(session, **base, positionIdx=position_idx)
        attempts.append((position_idx, result))
        if ok:
            order_id = str(result.get("result", {}).get("orderId", ""))
            if not order_id:
                raise LifecycleError("limit order placed but orderId missing")
            return position_idx, order_id

    raise LifecycleError(
        "failed to place limit order in one-way or hedge mode: "
        f"{summarize_place_attempts(attempts)}"
    )


def find_order(session: HTTP, symbol: str, order_id: str) -> dict[str, Any] | None:
    for order in get_open_orders(session, symbol):
        if str(order.get("orderId", "")) == order_id:
            return order
    return None


def order_fill_state(open_order: dict[str, Any] | None, positions: list[dict[str, Any]]) -> dict[str, Any]:
    position_qty = decimal_from(positions[0].get("size")) if positions else Decimal("0")
    remaining_qty = decimal_from(open_order.get("leavesQty")) if open_order else Decimal("0")
    cumulative_qty = decimal_from(open_order.get("cumExecQty")) if open_order else Decimal("0")
    filled_qty = max(position_qty, cumulative_qty)
    order_status = str(open_order.get("orderStatus", "FILLED_OR_CLOSED")) if open_order else "FILLED_OR_CLOSED"

    return {
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "order_status": order_status,
        "partial_observed": filled_qty > 0 and remaining_qty > 0,
    }


def observe_order_fill(
    session: HTTP,
    symbol: str,
    order_id: str,
    *,
    wait_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    deadline = time.time() + max(wait_s, 0.1)
    poll_interval_s = min(max(poll_interval_s, 0.05), 1.0)
    polls = 0
    last_state: dict[str, Any] = {
        "filled_qty": Decimal("0"),
        "remaining_qty": Decimal("0"),
        "order_status": "UNKNOWN",
        "partial_observed": False,
    }

    while time.time() < deadline:
        polls += 1
        open_order = find_order(session, symbol, order_id)
        positions = get_positions(session, symbol)
        last_state = order_fill_state(open_order, positions)
        last_state["open_order"] = open_order
        last_state["positions"] = positions
        last_state["polls"] = polls

        if last_state["partial_observed"]:
            return last_state
        if open_order is None and last_state["filled_qty"] > 0:
            return last_state

        time.sleep(poll_interval_s)

    return last_state


def get_ask_levels(session: HTTP, symbol: str, *, limit: int = 1) -> list[tuple[Decimal, Decimal]]:
    res = api_call(session.get_orderbook, category=CATEGORY, symbol=symbol, limit=limit)
    asks = res.get("result", {}).get("a", [])
    if not asks:
        raise LifecycleError(f"{symbol}: orderbook ask side is empty")

    levels: list[tuple[Decimal, Decimal]] = []
    for ask in asks:
        price = decimal_from(ask[0])
        size = decimal_from(ask[1])
        if price > 0 and size > 0:
            levels.append((price, size))

    if not levels:
        raise LifecycleError(f"{symbol}: orderbook ask side has no positive levels")
    return levels


def get_top_ask(session: HTTP, symbol: str) -> tuple[Decimal, Decimal]:
    return get_ask_levels(session, symbol, limit=1)[0]


def orderbook_limit_for(price_levels: int, requested_depth: int) -> int:
    if price_levels <= 1:
        return 1
    return max(50, price_levels, requested_depth)


def price_level_sweep(price_levels: int) -> list[int]:
    deepest = max(1, price_levels)
    return list(range(deepest, 0, -1))


def unique_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for symbol in symbols:
        clean = str(symbol).strip().upper()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)

    return result


def parse_symbol_csv(value: str) -> list[str]:
    return unique_symbols([item for item in str(value or "").split(",") if item.strip()])


def plan_partial_fill_order(
    *,
    instrument: dict[str, Decimal],
    ask_price: Decimal,
    ask_size: Decimal,
    max_notional: Decimal,
    target_notional_pct: Decimal = Decimal("0.95"),
    ask_levels: list[tuple[Decimal, Decimal]] | None = None,
    price_levels: int = 1,
) -> dict[str, Any]:
    qty_step = instrument["qty_step"]
    min_notional = instrument["min_notional"]
    min_qty = instrument["min_qty"]

    selected_levels: list[tuple[Decimal, Decimal]] = []
    if ask_levels:
        selected_count = max(1, min(price_levels, len(ask_levels)))
        selected_levels = ask_levels[:selected_count]
        ask_price = selected_levels[-1][0]
        ask_size = sum((size for _, size in selected_levels), Decimal("0"))

    ask_notional = sum((price * size for price, size in selected_levels), Decimal("0"))
    if ask_notional <= 0:
        ask_notional = ask_size * ask_price

    if ask_price <= 0 or ask_size <= 0:
        return {
            "eligible": False,
            "reason": "empty_top_ask",
            "ask_notional": decimal_to_str(ask_notional),
        }

    pct = max(Decimal("0.01"), min(Decimal("1"), target_notional_pct))
    target_notional = max_notional * pct
    target_qty = round_step(target_notional / ask_price, qty_step, "down")

    minimum_partial_qty = round_step(ask_size + qty_step, qty_step, "up")
    target_qty = max(target_qty, minimum_partial_qty)

    if target_qty < min_qty:
        target_qty = min_qty

    if min_notional > 0 and target_qty * ask_price < min_notional:
        target_qty = round_step((min_notional / ask_price) * Decimal("1.05"), qty_step, "up")

    notional = target_qty * ask_price
    if target_qty <= ask_size:
        return {
            "eligible": False,
            "reason": "top_ask_too_large",
            "ask_notional": decimal_to_str(ask_notional),
            "required_notional": decimal_to_str(notional),
            "target_qty": decimal_to_str(target_qty),
            "limit_price": decimal_to_str(ask_price),
            "visible_qty": decimal_to_str(ask_size),
        }

    if notional > max_notional:
        return {
            "eligible": False,
            "reason": "top_ask_too_large",
            "ask_notional": decimal_to_str(ask_notional),
            "required_notional": decimal_to_str(notional),
            "target_qty": decimal_to_str(target_qty),
            "limit_price": decimal_to_str(ask_price),
            "visible_qty": decimal_to_str(ask_size),
        }

    return {
        "eligible": True,
        "ask_notional": decimal_to_str(ask_notional),
        "required_notional": decimal_to_str(notional),
        "target_qty": decimal_to_str(target_qty),
        "limit_price": decimal_to_str(ask_price),
        "visible_qty": decimal_to_str(ask_size),
        "price_levels": str(max(1, min(price_levels, len(ask_levels) if ask_levels else 1))),
    }


def _ticker_turnover(item: dict[str, Any]) -> Decimal:
    value = decimal_from(
        item.get("turnover24h")
        or item.get("volume24h")
        or item.get("turnover")
        or "0"
    )
    return value if value > 0 else Decimal("1E+50")


def discover_partial_fill_candidates(
    session: HTTP,
    *,
    max_notional: Decimal,
    limit: int,
    max_scan: int,
    target_notional_pct: Decimal,
    price_levels: int,
    orderbook_depth: int,
    quote_suffix: str = "USDT",
) -> list[dict[str, Any]]:
    if limit <= 0 or max_scan <= 0:
        return []

    res = api_call(session.get_tickers, category=CATEGORY)
    tickers = list(res.get("result", {}).get("list", []))
    tickers = sorted(tickers, key=_ticker_turnover)

    candidates: list[dict[str, Any]] = []
    scanned = 0
    quote_suffix = quote_suffix.upper()

    for ticker in tickers:
        if scanned >= max_scan or len(candidates) >= limit:
            break

        symbol = str(ticker.get("symbol", "")).upper()
        if not symbol.endswith(quote_suffix):
            continue

        scanned += 1
        try:
            instrument = get_instrument(session, symbol)
            levels = get_ask_levels(
                session,
                symbol,
                limit=orderbook_limit_for(price_levels, orderbook_depth),
            )
            ask_price, ask_size = levels[0]
            plan = None
            for candidate_price_levels in price_level_sweep(price_levels):
                candidate_plan = plan_partial_fill_order(
                    instrument=instrument,
                    ask_price=ask_price,
                    ask_size=ask_size,
                    max_notional=max_notional,
                    target_notional_pct=target_notional_pct,
                    ask_levels=levels,
                    price_levels=candidate_price_levels,
                )
                if candidate_plan.get("eligible"):
                    plan = candidate_plan
                    break

            if plan is None:
                continue

            candidates.append(
                {
                    "symbol": symbol,
                    "ask_price": plan["limit_price"],
                    "top_ask_price": decimal_to_str(ask_price),
                    "top_ask_qty": decimal_to_str(ask_size),
                    "visible_qty": plan["visible_qty"],
                    "ask_notional": plan["ask_notional"],
                    "required_notional": plan["required_notional"],
                    "target_qty": plan["target_qty"],
                    "price_levels": plan["price_levels"],
                }
            )
        except Exception:
            continue

    return candidates


def run_partial_fill_probe(
    session: HTTP,
    *,
    symbols: list[str],
    max_notional: Decimal,
    target_notional_pct: Decimal,
    price_levels: int,
    orderbook_depth: int,
    candidate_plans: dict[str, dict[str, Any]] | None = None,
    prefix: str,
    wait_s: float,
    poll_interval_s: float,
) -> dict[str, Any]:
    skipped: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    for symbol in symbols:
        symbol = symbol.upper()
        try:
            if get_positions(session, symbol) or get_open_orders(session, symbol):
                skipped.append({"symbol": symbol, "reason": "not_flat"})
                continue

            instrument = get_instrument(session, symbol)
            symbol_plan = (candidate_plans or {}).get(symbol)
            symbol_price_levels = int(symbol_plan.get("price_levels", price_levels)) if symbol_plan else price_levels
            levels = get_ask_levels(
                session,
                symbol,
                limit=orderbook_limit_for(symbol_price_levels, orderbook_depth),
            )
            top_ask_price, top_ask_size = levels[0]
            plan = plan_partial_fill_order(
                instrument=instrument,
                ask_price=top_ask_price,
                ask_size=top_ask_size,
                max_notional=max_notional,
                target_notional_pct=target_notional_pct,
                ask_levels=levels,
                price_levels=symbol_price_levels,
            )

            if not plan.get("eligible"):
                skipped.append(
                    {
                        "symbol": symbol,
                        "reason": plan.get("reason", "not_eligible"),
                        "ask_notional": plan.get("ask_notional"),
                        "required_notional": plan.get("required_notional"),
                        "visible_qty": plan.get("visible_qty"),
                        "limit_price": plan.get("limit_price"),
                    }
                )
                continue

            target_qty = decimal_from(plan["target_qty"])
            limit_price = decimal_from(plan["limit_price"])
            order_link_id = f"{prefix}-pf-{symbol}"[:36]
            position_idx, order_id = place_far_limit_with_mode_fallback(
                session,
                symbol=symbol,
                qty=target_qty,
                price=limit_price,
                order_link_id=order_link_id,
                time_in_force="GTC",
            )

            observed = observe_order_fill(
                session,
                symbol,
                order_id,
                wait_s=wait_s,
                poll_interval_s=poll_interval_s,
            )
            open_order = observed.get("open_order")
            filled_qty = observed["filled_qty"]
            remaining_qty = observed["remaining_qty"]
            order_status = observed["order_status"]
            partial_observed = bool(observed["partial_observed"])

            if open_order:
                api_call(session.cancel_order, category=CATEGORY, symbol=symbol, orderId=order_id)
                time.sleep(0.5)

            if filled_qty > 0:
                close_open_positions(session, symbol)
                wait_for_position(session, symbol, want_open=False, timeout_s=wait_s)

            assert_symbol_flat(session, symbol)
            attempt = {
                "name": "partial_fill_probe",
                "symbol": symbol,
                "position_idx": position_idx,
                "order_id": order_id,
                "ask_price": decimal_to_str(limit_price),
                "top_ask_price": decimal_to_str(top_ask_price),
                "top_ask_qty": decimal_to_str(top_ask_size),
                "visible_qty": plan["visible_qty"],
                "price_levels": plan["price_levels"],
                "order_qty": decimal_to_str(target_qty),
                "filled_qty": decimal_to_str(filled_qty),
                "remaining_qty": decimal_to_str(remaining_qty),
                "order_status": order_status,
                "partial_observed": partial_observed,
                "observation_polls": observed.get("polls", 0),
                "skipped_before": list(skipped),
            }
            attempts.append(attempt)

            if partial_observed:
                return attempt

        except Exception as exc:
            try:
                cancel_prefix_orders(session, symbol, f"{prefix}-pf")
                close_open_positions(session, symbol)
            except Exception:
                pass
            error = str(exc)
            reason = classify_probe_error(error)
            skipped.append({"symbol": symbol, "reason": reason, "error": error[:240]})

    return {
        "name": "partial_fill_probe",
        "partial_observed": False,
        "attempts": attempts,
        "skipped": skipped,
    }


def run_retcode_matrix(
    session: HTTP,
    *,
    symbol: str,
    qty: Decimal,
    price: Decimal,
    position_idx: int,
    prefix: str,
) -> list[dict[str, Any]]:
    wrong_idx = 0 if position_idx != 0 else 1
    cases: list[dict[str, Any]] = []

    ok, result = try_place_order(
        session,
        category=CATEGORY,
        symbol=symbol,
        side="Buy",
        orderType="Limit",
        qty=decimal_to_str(qty),
        price=decimal_to_str(price),
        timeInForce="PostOnly",
        positionIdx=wrong_idx,
        orderLinkId=f"{prefix}-ret-wrong-idx",
    )
    if ok:
        order_id = str(result.get("result", {}).get("orderId", ""))
        if order_id:
            api_call(session.cancel_order, category=CATEGORY, symbol=symbol, orderId=order_id)
        raise LifecycleError("retCode matrix expected wrong positionIdx to fail, but it placed an order")

    cases.append(
        {
            "name": "wrong_position_idx",
            "expected_failure": True,
            **summarize_failure(result),
        }
    )

    ok, result = try_place_order(
        session,
        category=CATEGORY,
        symbol=symbol,
        side="Buy",
        orderType="Limit",
        qty="0",
        price=decimal_to_str(price),
        timeInForce="PostOnly",
        positionIdx=position_idx,
        orderLinkId=f"{prefix}-ret-zero-qty",
    )
    if ok:
        order_id = str(result.get("result", {}).get("orderId", ""))
        if order_id:
            api_call(session.cancel_order, category=CATEGORY, symbol=symbol, orderId=order_id)
        raise LifecycleError("retCode matrix expected zero qty to fail, but it placed an order")

    cases.append(
        {
            "name": "zero_qty",
            "expected_failure": True,
            **summarize_failure(result),
        }
    )

    ok, result = try_api_call(
        session.cancel_order,
        category=CATEGORY,
        symbol=symbol,
        orderId="00000000-0000-0000-0000-000000000000",
    )
    if ok:
        raise LifecycleError("retCode matrix expected fake cancel to fail, but it succeeded")

    cases.append(
        {
            "name": "fake_cancel",
            "expected_failure": True,
            **summarize_failure(result),
        }
    )

    return cases


def run_restart_recovery(
    *,
    symbol: str,
    side: str,
    entry_price: Decimal,
    qty: Decimal,
    stop_loss: Decimal,
    order_id: str,
    db_path: Path,
) -> dict[str, Any]:
    db_sync = DatabaseSync(db_path=str(db_path))
    saved = db_sync.save_open_trade(
        symbol=symbol,
        side=side,
        entry=float(entry_price),
        qty=float(qty),
        sl=float(stop_loss),
        score=0,
        poi_type="BYBIT_DEMO_LIFECYCLE",
        order_id=order_id,
        status="OPEN",
    )
    if not saved:
        raise LifecycleError("restart recovery failed to seed local DB")

    if db_sync._db is not None:
        db_sync._db.close()

    from core.exchange import ExchangeManager

    exchange = ExchangeManager()
    restarted_db = TradeDatabase(str(db_path))
    exchange.sync_db_with_exchange(restarted_db)
    recovered = restarted_db.get_open_trade(symbol=symbol, side=side)

    if not recovered:
        raise LifecycleError("restart recovery did not preserve visible exchange position as OPEN")

    try:
        restarted_db.close()
    except Exception:
        pass

    return {
        "name": "restart_recovery_sync",
        "db_path": str(db_path),
        "status": str(recovered.get("status", "")),
        "qty": float(recovered.get("qty", 0.0) or 0.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XRPUSDT")
    parser.add_argument("--max-notional", type=Decimal, default=Decimal("25"))
    parser.add_argument("--wait", type=float, default=12.0)
    parser.add_argument("--partial-fill-symbols", default="WIFUSDT,NEARUSDT,RENDERUSDT,OPUSDT")
    parser.add_argument("--partial-fill-dynamic-candidates", type=int, default=8)
    parser.add_argument("--partial-fill-max-scan", type=int, default=80)
    parser.add_argument("--partial-fill-target-notional-pct", type=Decimal, default=Decimal("0.95"))
    parser.add_argument("--partial-fill-price-levels", type=int, default=1)
    parser.add_argument("--partial-fill-orderbook-depth", type=int, default=50)
    parser.add_argument("--partial-fill-poll-interval", type=float, default=0.2)
    parser.add_argument("--partial-fill-probe-only", action="store_true")
    parser.add_argument("--skip-partial-close", action="store_true")
    parser.add_argument("--skip-partial-fill-probe", action="store_true")
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
    db_path = Path(tempfile.gettempdir()) / f"cryptobot_bybit_demo_lifecycle_{prefix}.db"
    opened_position = False
    summary: dict[str, Any] = {
        "status": "STARTED",
        "environment": {"demo": config.BYBIT_DEMO, "testnet": config.BYBIT_TESTNET},
        "symbol": symbol,
        "prefix": prefix,
        "steps": [],
    }

    try:
        partial_symbols = unique_symbols(parse_symbol_csv(args.partial_fill_symbols) + [symbol])
        if not args.skip_partial_fill_probe:
            discovery: list[dict[str, Any]] = []
            if args.partial_fill_dynamic_candidates > 0:
                discovery = discover_partial_fill_candidates(
                    session,
                    max_notional=args.max_notional,
                    limit=args.partial_fill_dynamic_candidates,
                    max_scan=args.partial_fill_max_scan,
                    target_notional_pct=args.partial_fill_target_notional_pct,
                    price_levels=args.partial_fill_price_levels,
                    orderbook_depth=args.partial_fill_orderbook_depth,
                )
                summary["steps"].append(
                    {
                        "name": "partial_fill_candidate_discovery",
                        "candidates": discovery,
                    }
                )

            partial_symbols = unique_symbols(
                [item["symbol"] for item in discovery]
                + partial_symbols
            )
            candidate_plans = {str(item["symbol"]).upper(): item for item in discovery}
            summary["steps"].append(
                run_partial_fill_probe(
                    session,
                    symbols=partial_symbols,
                    max_notional=args.max_notional,
                    target_notional_pct=args.partial_fill_target_notional_pct,
                    price_levels=args.partial_fill_price_levels,
                    orderbook_depth=args.partial_fill_orderbook_depth,
                    candidate_plans=candidate_plans,
                    prefix=prefix,
                    wait_s=args.wait,
                    poll_interval_s=args.partial_fill_poll_interval,
                )
            )

            if args.partial_fill_probe_only:
                summary["status"] = "OK"
                print(json.dumps(summary, indent=2, sort_keys=True))
                return 0

        if get_positions(session, symbol):
            raise LifecycleError(f"{symbol}: active position exists before lifecycle; aborting")
        if get_open_orders(session, symbol):
            raise LifecycleError(f"{symbol}: open orders exist before lifecycle; aborting")

        instrument = get_instrument(session, symbol)
        last_price = get_last_price(session, symbol)
        tick_size = instrument["tick_size"]

        far_limit_price = round_step(last_price * Decimal("0.80"), tick_size, "down")
        qty = choose_qty(
            last_price,
            far_limit_price,
            instrument,
            args.max_notional,
            require_partial_close=not args.skip_partial_close,
        )

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

        amended_price = round_step(last_price * Decimal("0.79"), tick_size, "down")
        api_call(
            session.amend_order,
            category=CATEGORY,
            symbol=symbol,
            orderId=pending_order_id,
            price=decimal_to_str(amended_price),
        )
        time.sleep(0.5)
        amended_order = find_order(session, symbol, pending_order_id)
        if not amended_order:
            raise LifecycleError("amended limit order disappeared from open orders")
        visible_price = decimal_from(amended_order.get("price"))
        if visible_price != amended_price:
            raise LifecycleError(
                f"amended price mismatch: expected {decimal_to_str(amended_price)}, "
                f"got {decimal_to_str(visible_price)}"
            )
        summary["steps"].append(
            {
                "name": "limit_amend",
                "order_id": pending_order_id,
                "price": decimal_to_str(amended_price),
            }
        )

        summary["steps"].append(
            {
                "name": "retcode_matrix",
                "cases": run_retcode_matrix(
                    session,
                    symbol=symbol,
                    qty=qty,
                    price=amended_price,
                    position_idx=position_idx,
                    prefix=prefix,
                ),
            }
        )

        api_call(session.cancel_order, category=CATEGORY, symbol=symbol, orderId=pending_order_id)
        summary["steps"].append({"name": "limit_cancel", "order_id": pending_order_id})
        time.sleep(0.5)

        open_ids_after_cancel = {str(order.get("orderId", "")) for order in get_open_orders(session, symbol)}
        if pending_order_id in open_ids_after_cancel:
            raise LifecycleError("cancelled limit order is still visible in open orders")

        open_link = f"{prefix}-open"
        open_res = api_call(
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
        open_order_id = str(open_res.get("result", {}).get("orderId", ""))
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

        if not args.skip_partial_close:
            partial_qty = round_step(open_qty / Decimal("2"), instrument["qty_step"], "down")
            if partial_qty <= 0 or partial_qty >= open_qty:
                raise LifecycleError(f"invalid partial close qty: {decimal_to_str(partial_qty)}")

            api_call(
                session.place_order,
                category=CATEGORY,
                symbol=symbol,
                side="Sell",
                orderType="Market",
                qty=decimal_to_str(partial_qty),
                reduceOnly=True,
                timeInForce="IOC",
                orderLinkId=f"{prefix}-partial-close",
                positionIdx=position_idx,
            )

            deadline = time.time() + args.wait
            remaining_positions: list[dict[str, Any]] = []
            while time.time() < deadline:
                remaining_positions = get_positions(session, symbol)
                if remaining_positions:
                    remaining_qty = decimal_from(remaining_positions[0].get("size"))
                    if Decimal("0") < remaining_qty < open_qty:
                        open_qty = remaining_qty
                        break
                time.sleep(0.5)
            else:
                raise LifecycleError(f"partial close did not leave a smaller open position: {remaining_positions}")

            summary["steps"].append(
                {
                    "name": "partial_reduce_only_close",
                    "closed_qty": decimal_to_str(partial_qty),
                    "remaining_qty": decimal_to_str(open_qty),
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

        summary["steps"].append(
            run_restart_recovery(
                symbol=symbol,
                side="LONG",
                entry_price=entry_price,
                qty=open_qty,
                stop_loss=sl_price,
                order_id=open_order_id or open_link,
                db_path=db_path,
            )
        )

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

    except LifecycleError as exc:
        summary["status"] = "ERROR"
        summary["error"] = str(exc)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1

    finally:
        cleanup = {"prefix_orders_cancelled": 0, "positions_closed": 0}
        if not args.partial_fill_probe_only:
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
