from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


DECISION_WATCH_ONLY = "WATCH_ONLY"
ALT_EXECUTION_READY = "READY"
ALT_EXECUTION_REJECTED = "REJECTED"

SUPPORTED_EXECUTION_STRATEGIES = frozenset(
    {
        "MEAN_REVERSION",
        "BREAKOUT",
        "TREND_PULLBACK",
        "VOLATILITY_EXPANSION",
    }
)

VALID_EXECUTION_SIDES = {"LONG", "SHORT"}
VALID_ORDER_TYPES = {"LIMIT": "Limit", "MARKET": "Market"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default

    if not math.isfinite(numeric):
        return default

    return numeric


def _safe_int(value: Any, default: int = 0) -> int:
    numeric = _safe_float(value, float(default))
    return int(round(numeric))


def normalize_strategy_name(value: Any) -> str:
    return str(value or "").strip().upper()


def parse_allowed_strategies(raw: Any) -> set[str]:
    if raw is None or raw == "":
        return set(SUPPORTED_EXECUTION_STRATEGIES)

    if isinstance(raw, str):
        parts: Iterable[Any] = raw.split(",")
    elif isinstance(raw, Iterable):
        parts = raw
    else:
        raise ValueError("allowed strategies must be a CSV string or iterable")

    names = {normalize_strategy_name(part) for part in parts if str(part).strip()}
    if not names or names.intersection({"*", "ALL"}):
        return set(SUPPORTED_EXECUTION_STRATEGIES)

    unknown = names.difference(SUPPORTED_EXECUTION_STRATEGIES)
    if unknown:
        joined = ",".join(sorted(unknown))
        raise ValueError(f"unknown strategy name(s): {joined}")

    return names


def _reject(decision: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    decision = decision if isinstance(decision, dict) else {}
    return {
        "status": ALT_EXECUTION_REJECTED,
        "reason": reason,
        "strategy": normalize_strategy_name(decision.get("selected_strategy")),
        "symbol": decision.get("symbol"),
        "decision": decision.get("decision"),
    }


def build_strategy_execution_plan(
    decision: dict[str, Any],
    *,
    allowed_strategies: Any = None,
    min_rr: float = 1.2,
) -> dict[str, Any]:
    if not isinstance(decision, dict):
        return _reject(None, "missing_decision")

    if decision.get("decision") != DECISION_WATCH_ONLY:
        return _reject(decision, "decision_not_watch_only")

    strategy = normalize_strategy_name(decision.get("selected_strategy"))
    if strategy not in parse_allowed_strategies(allowed_strategies):
        return _reject(decision, "strategy_not_allowed")

    side = str(decision.get("side", "")).strip().upper()
    if side not in VALID_EXECUTION_SIDES:
        return _reject(decision, "invalid_side")

    plan = decision.get("plan")
    if not isinstance(plan, dict):
        return _reject(decision, "missing_plan")

    order_type_key = str(plan.get("order_type", "")).strip().upper()
    order_type = VALID_ORDER_TYPES.get(order_type_key)
    if order_type is None:
        return _reject(decision, "invalid_order_type")

    entry = _safe_float(plan.get("entry"))
    stop_loss = _safe_float(plan.get("stop_loss"))
    target = _safe_float(plan.get("target"))
    rr = _safe_float(plan.get("rr"))

    if not all(value > 0 for value in (entry, stop_loss, target, rr)):
        return _reject(decision, "invalid_plan_values")

    if rr < float(min_rr):
        return _reject(decision, "rr_below_execution_min")

    if side == "LONG" and not (stop_loss < entry < target):
        return _reject(decision, "invalid_directional_plan")

    if side == "SHORT" and not (target < entry < stop_loss):
        return _reject(decision, "invalid_directional_plan")

    score = _safe_int(decision.get("score"))
    threshold = _safe_int(decision.get("threshold"))
    if score < threshold:
        return _reject(decision, "score_below_threshold")

    return {
        "status": ALT_EXECUTION_READY,
        "reason": "ready",
        "strategy": strategy,
        "symbol": decision.get("symbol"),
        "side": side,
        "score": score,
        "threshold": threshold,
        "order_type": order_type,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "rr": rr,
    }


def build_strategy_poi(execution_plan: dict[str, Any]) -> dict[str, Any]:
    entry = _safe_float(execution_plan.get("entry"))
    stop_loss = _safe_float(execution_plan.get("stop_loss"))
    strategy = normalize_strategy_name(execution_plan.get("strategy")) or "UNKNOWN"

    return {
        "type": f"ALT_{strategy}",
        "side": str(execution_plan.get("side", "")).upper(),
        "price": entry,
        "mid": entry,
        "top": max(entry, stop_loss),
        "bottom": min(entry, stop_loss),
    }


def build_single_target_tp_levels(execution_plan: dict[str, Any]) -> dict[str, float]:
    return {"tp1": _safe_float(execution_plan.get("target"))}
