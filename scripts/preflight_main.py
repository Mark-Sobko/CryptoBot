from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _apply_profile_defaults(profile: str, warnings: list[str]) -> dict[str, str | None]:
    if not profile:
        return {}

    try:
        from scripts.run_main_profile import PROFILE_DEFAULTS
    except Exception as exc:
        warnings.append(f"profile_defaults_unavailable:{type(exc).__name__}:{exc}")
        return {}

    defaults = PROFILE_DEFAULTS.get(profile)
    if defaults is None:
        warnings.append(f"unknown_profile:{profile}")
        return {}

    original_values: dict[str, str | None] = {}
    for key, value in defaults.items():
        if key not in os.environ:
            original_values[key] = None
            os.environ[key] = value
    return original_values


def _restore_env(original_values: dict[str, str | None]) -> None:
    for key, value in original_values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _check_writable_dir(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".preflight-", dir=path, delete=True):
            pass
        return True, ""
    except Exception as exc:
        return False, f"{path}: {type(exc).__name__}: {exc}"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if math.isfinite(numeric) else default


def _position_has_stop(position: dict[str, Any]) -> bool:
    return _safe_float(
        position.get("stop_loss", position.get("stopLoss")),
        0.0,
    ) > 0


def _order_notional(order: dict[str, Any]) -> float:
    for key in ("leavesValue", "cumExecValue", "orderValue"):
        value = _safe_float(order.get(key), 0.0)
        if value > 0:
            return value

    qty = _safe_float(order.get("leavesQty", order.get("qty")), 0.0)
    price = _safe_float(order.get("price", order.get("lastPriceOnCreated")), 0.0)
    return qty * price if qty > 0 and price > 0 else 0.0


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _decimal_from_float(value: float) -> Decimal:
    return Decimal(str(value))


def _normalise_stop_price(raw_price: float, tick_size: float, side: str) -> str:
    try:
        price = _decimal_from_float(raw_price)
        tick = _decimal_from_float(tick_size)
        if price <= 0 or tick <= 0:
            return ""
    except (InvalidOperation, ValueError):
        return ""

    rounding = ROUND_FLOOR if str(side).upper() in {"BUY", "LONG"} else ROUND_CEILING
    ticks = (price / tick).to_integral_value(rounding=rounding)
    normalised = ticks * tick
    return _format_decimal(normalised) if normalised > 0 else ""


def _position_reference_price(position: dict[str, Any]) -> float:
    for key in ("entry_price", "entryPrice", "avgPrice", "mark_price", "markPrice"):
        price = _safe_float(position.get(key), 0.0)
        if price > 0:
            return price
    return 0.0


def _position_emergency_stop(
    exchange: Any,
    position: dict[str, Any],
    *,
    emergency_stop_pct: float,
) -> str:
    symbol = str(position.get("symbol", "") or "")
    side = str(position.get("side", "") or "")
    reference_price = _position_reference_price(position)
    if not symbol or reference_price <= 0:
        return ""

    info = exchange.get_ticker_info(symbol) if hasattr(exchange, "get_ticker_info") else None
    tick_size = _safe_float((info or {}).get("tickSize"), 0.0)
    if tick_size <= 0:
        tick_size = reference_price * 0.0001

    stop_distance = reference_price * max(emergency_stop_pct, 0.0) / 100.0
    if stop_distance <= 0:
        return ""

    if str(side).upper() in {"BUY", "LONG"}:
        raw_stop = reference_price - stop_distance
    else:
        raw_stop = reference_price + stop_distance

    return _normalise_stop_price(raw_stop, tick_size, side)


def _exchange_call(exchange: Any, method_name: str, **kwargs: Any) -> bool:
    session = getattr(exchange, "session", None)
    method = getattr(session, method_name, None)
    if method is None:
        return False

    if hasattr(exchange, "_request_with_retry"):
        result = exchange._request_with_retry(method, **kwargs)
    else:
        result = method(**kwargs)

    return isinstance(result, dict) and result.get("retCode") == 0


def _repair_exchange_state(
    exchange: Any,
    *,
    active_positions: list[dict[str, Any]],
    pending_entry_orders: list[dict[str, Any]],
    max_order_notional_usd: float,
    emergency_stop_pct: float = 2.0,
) -> tuple[list[dict[str, Any]], list[str]]:
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    category = getattr(exchange, "CATEGORY", "linear")

    for position in active_positions:
        if _position_has_stop(position):
            continue

        symbol = str(position.get("symbol", "") or "")
        position_idx = int(position.get("positionIdx", 0) or 0)
        stop_loss = _position_emergency_stop(
            exchange,
            position,
            emergency_stop_pct=emergency_stop_pct,
        )
        action = {
            "action": "set_emergency_stop_loss",
            "symbol": symbol,
            "positionIdx": position_idx,
            "stop_loss": stop_loss,
            "status": "FAILED",
        }

        if symbol and stop_loss and position_idx >= 0:
            ok = _exchange_call(
                exchange,
                "set_trading_stop",
                category=category,
                symbol=symbol,
                stopLoss=stop_loss,
                slTriggerBy="LastPrice",
                positionIdx=position_idx,
            )
            if ok:
                action["status"] = "OK"

        if action["status"] != "OK":
            errors.append(f"exchange_repair_failed:set_stop_loss:{symbol}")
        actions.append(action)

    if max_order_notional_usd > 0:
        for order in pending_entry_orders:
            notional = _order_notional(order)
            if notional <= max_order_notional_usd:
                continue

            symbol = str(order.get("symbol", "") or "")
            order_id = str(order.get("orderId", "") or "")
            action = {
                "action": "cancel_oversized_pending_entry_order",
                "symbol": symbol,
                "orderId": order_id,
                "notional": round(notional, 8),
                "max_order_notional_usd": max_order_notional_usd,
                "status": "FAILED",
            }
            if symbol and order_id:
                ok = _exchange_call(
                    exchange,
                    "cancel_order",
                    category=category,
                    symbol=symbol,
                    orderId=order_id,
                )
                if ok:
                    action["status"] = "OK"

            if action["status"] != "OK":
                errors.append(f"exchange_repair_failed:cancel_order:{symbol}:{order_id}")
            actions.append(action)

    return actions, errors


def _exchange_safety_errors(
    *,
    active_positions: list[dict[str, Any]],
    pending_entry_orders: list[dict[str, Any]],
    max_order_notional_usd: float,
    execution_enabled: bool,
) -> list[str]:
    if not execution_enabled:
        return []

    errors: list[str] = []

    for position in active_positions:
        if _position_has_stop(position):
            continue
        symbol = str(position.get("symbol", "UNKNOWN") or "UNKNOWN")
        side = str(position.get("side", "") or "")
        position_idx = str(position.get("positionIdx", "") or "")
        errors.append(
            f"active_position_without_stop_loss:{symbol}:{side}:positionIdx={position_idx}"
        )

    if max_order_notional_usd > 0:
        for order in pending_entry_orders:
            notional = _order_notional(order)
            if notional <= max_order_notional_usd:
                continue

            symbol = str(order.get("symbol", "UNKNOWN") or "UNKNOWN")
            order_id = str(order.get("orderId", "") or "")
            errors.append(
                "pending_entry_order_exceeds_max_notional:"
                f"{symbol}:{order_id}:{notional:.4f}>{max_order_notional_usd:.4f}"
            )

    return errors


def run_preflight(*, profile: str = "", exchange_check: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    exchange: dict[str, Any] | None = None
    original_env = _apply_profile_defaults(profile, warnings)

    try:
        import config
    except Exception as exc:
        _restore_env(original_env)
        return {
            "status": "FAILED",
            "profile": profile,
            "errors": [f"config_import_failed:{type(exc).__name__}:{exc}"],
            "warnings": warnings,
        }
    finally:
        _restore_env(original_env)

    global_cfg = config.RISK_MANAGEMENT.get("global", {})
    execution_enabled = bool(global_cfg.get("execution_enabled", True))
    multi_strategy_execution_enabled = bool(
        global_cfg.get("multi_strategy_execution_enabled", False)
    )
    allow_live = bool(global_cfg.get("allow_live_trading", False))

    if not config.BYBIT_DEMO and not config.BYBIT_TESTNET and not allow_live:
        errors.append("live_mode_refused_without_ALLOW_LIVE_TRADING")

    if config.BYBIT_DEMO and config.BYBIT_TESTNET:
        errors.append("BYBIT_DEMO_and_BYBIT_TESTNET_cannot_both_be_true")

    if not config.API_KEY or not config.API_SECRET:
        errors.append("missing_BYBIT_API_KEY_or_BYBIT_API_SECRET")

    if multi_strategy_execution_enabled and not execution_enabled:
        errors.append("MULTI_STRATEGY_EXECUTION_ENABLED_requires_EXECUTION_ENABLED")

    max_runtime_minutes = _safe_float(global_cfg.get("max_runtime_minutes"), 0.0)
    max_orders_per_run = int(global_cfg.get("max_orders_per_run", 0) or 0)
    max_orders_per_cycle = int(global_cfg.get("max_orders_per_cycle", 0) or 0)
    max_order_notional_usd = _safe_float(global_cfg.get("max_order_notional_usd"), 0.0)

    if execution_enabled:
        if max_runtime_minutes <= 0:
            warnings.append("MAX_RUNTIME_MINUTES_is_unlimited")
        if max_orders_per_run <= 0:
            warnings.append("MAX_ORDERS_PER_RUN_is_unlimited")
        if max_orders_per_cycle <= 0:
            warnings.append("MAX_ORDERS_PER_CYCLE_is_unlimited")
        if max_order_notional_usd <= 0:
            warnings.append("MAX_ORDER_NOTIONAL_USD_is_unlimited")

    for label, path in {
        "data_dir": Path(config.DATA_DIR),
        "logs_dir": Path(config.LOGS_DIR),
        "db_parent": Path(config.DB_PATH).parent,
    }.items():
        ok, message = _check_writable_dir(path)
        if not ok:
            errors.append(f"{label}_not_writable:{message}")

    repair_exchange_state = os.environ.get("PREFLIGHT_REPAIR_EXCHANGE_STATE", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    emergency_stop_pct = _safe_float(os.environ.get("EXCHANGE_REPAIR_EMERGENCY_STOP_PCT"), 2.0)

    if exchange_check:
        try:
            from core.exchange import ExchangeManager

            ex = ExchangeManager()
            active_positions = ex.get_active_positions()
            pending_entry_orders = ex.get_pending_entry_orders()
            if pending_entry_orders is None:
                pending_entry_orders = []

            repair_actions: list[dict[str, Any]] = []
            repair_errors: list[str] = []
            if repair_exchange_state and not (config.BYBIT_DEMO or config.BYBIT_TESTNET):
                errors.append("exchange_repair_refused_outside_demo_or_testnet")
            elif repair_exchange_state and execution_enabled:
                repair_actions, repair_errors = _repair_exchange_state(
                    ex,
                    active_positions=active_positions,
                    pending_entry_orders=pending_entry_orders,
                    max_order_notional_usd=max_order_notional_usd,
                    emergency_stop_pct=emergency_stop_pct,
                )
                active_positions = ex.get_active_positions()
                pending_entry_orders = ex.get_pending_entry_orders()
                if pending_entry_orders is None:
                    pending_entry_orders = []

            exchange = {
                "total_balance": ex.get_total_balance(),
                "active_positions": active_positions,
                "pending_entry_orders": pending_entry_orders,
                "repair_actions": repair_actions,
            }
            errors.extend(repair_errors)
            errors.extend(
                _exchange_safety_errors(
                    active_positions=active_positions,
                    pending_entry_orders=pending_entry_orders,
                    max_order_notional_usd=max_order_notional_usd,
                    execution_enabled=execution_enabled,
                )
            )
        except Exception as exc:
            errors.append(f"exchange_check_failed:{type(exc).__name__}:{exc}")

    return {
        "status": "FAILED" if errors else "OK",
        "profile": profile,
        "environment": {
            "demo": bool(config.BYBIT_DEMO),
            "testnet": bool(config.BYBIT_TESTNET),
            "live_allowed": allow_live,
            "execution_enabled": execution_enabled,
            "multi_strategy_execution_enabled": multi_strategy_execution_enabled,
            "exchange_repair_enabled": bool(repair_exchange_state and exchange_check),
        },
        "guards": {
            "max_runtime_minutes": max_runtime_minutes,
            "max_orders_per_run": max_orders_per_run,
            "max_orders_per_cycle": max_orders_per_cycle,
            "max_order_notional_usd": max_order_notional_usd,
            "multi_strategy_health_guard_enabled": bool(
                global_cfg.get("multi_strategy_health_guard_enabled", True)
            ),
            "multi_strategy_health_window_minutes": _safe_float(
                global_cfg.get("multi_strategy_health_window_minutes"),
                0.0,
            ),
            "multi_strategy_health_max_rejections": int(
                global_cfg.get("multi_strategy_health_max_rejections", 0) or 0
            ),
            "multi_strategy_health_max_executor_failures": int(
                global_cfg.get("multi_strategy_health_max_executor_failures", 0) or 0
            ),
        },
        "paths": {
            "db": str(config.DB_PATH),
            "log": str(config.LOG_PATH),
            "strategy_observation": str(config.STRATEGY_OBSERVATION_PATH),
        },
        "exchange": exchange,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight checks before launching main.py.")
    parser.add_argument("--profile", default="")
    parser.add_argument(
        "--exchange",
        action="store_true",
        help="Also verify Bybit connectivity/account state. This is off by default.",
    )
    parser.add_argument(
        "--repair-exchange-state",
        action="store_true",
        help=(
            "With --exchange, repair demo/testnet state before launch: set emergency "
            "SL on positions without SL and cancel pending entry orders above the "
            "configured notional cap."
        ),
    )
    args = parser.parse_args()

    if args.repair_exchange_state and not args.exchange:
        parser.error("--repair-exchange-state requires --exchange")

    original_repair = os.environ.get("PREFLIGHT_REPAIR_EXCHANGE_STATE")
    if args.repair_exchange_state:
        os.environ["PREFLIGHT_REPAIR_EXCHANGE_STATE"] = "true"
    try:
        result = run_preflight(profile=args.profile, exchange_check=args.exchange)
    finally:
        if original_repair is None:
            os.environ.pop("PREFLIGHT_REPAIR_EXCHANGE_STATE", None)
        else:
            os.environ["PREFLIGHT_REPAIR_EXCHANGE_STATE"] = original_repair
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] == "OK":
        print("PREFLIGHT_OK")
        return 0
    print("PREFLIGHT_FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
