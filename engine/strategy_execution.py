from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
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


def parse_allowed_order_types(raw: Any) -> set[str]:
    if raw is None or raw == "":
        return set(VALID_ORDER_TYPES.values())

    if isinstance(raw, str):
        parts: Iterable[Any] = raw.split(",")
    elif isinstance(raw, Iterable):
        parts = raw
    else:
        raise ValueError("allowed order types must be a CSV string or iterable")

    order_types: set[str] = set()
    unknown: set[str] = set()
    for part in parts:
        key = str(part or "").strip().upper()
        if not key:
            continue

        canonical = VALID_ORDER_TYPES.get(key)
        if canonical is None:
            unknown.add(key)
            continue

        order_types.add(canonical)

    if unknown:
        joined = ",".join(sorted(unknown))
        raise ValueError(f"unknown order type(s): {joined}")

    return order_types or set(VALID_ORDER_TYPES.values())


def _ordered_order_types(order_types: set[str]) -> list[str]:
    return [
        canonical
        for canonical in VALID_ORDER_TYPES.values()
        if canonical in order_types
    ]


def resolve_strategy_execution_policy(
    strategy: Any,
    strategy_policies: Any = None,
    *,
    fallback_min_rr: float = 1.2,
) -> dict[str, Any]:
    normalized_strategy = normalize_strategy_name(strategy)
    policies = strategy_policies if isinstance(strategy_policies, Mapping) else {}
    raw_policy = policies.get(normalized_strategy, {})

    if raw_policy is None:
        raw_policy = {}

    if not isinstance(raw_policy, Mapping):
        raise ValueError("strategy policy must be a mapping")

    base_min_rr = _safe_float(fallback_min_rr, 1.2)
    if base_min_rr <= 0:
        base_min_rr = 1.2

    min_rr = _safe_float(raw_policy.get("min_rr"), base_min_rr)
    max_notional_usd = _safe_float(raw_policy.get("max_notional_usd"), 0.0)
    risk_pct_multiplier = _safe_float(raw_policy.get("risk_pct_multiplier"), 1.0)
    cooldown_minutes = _safe_float(raw_policy.get("cooldown_minutes"), 0.0)
    max_hold_minutes = _safe_float(raw_policy.get("max_hold_minutes"), 0.0)
    allowed_order_types = parse_allowed_order_types(raw_policy.get("allowed_order_types"))

    if not (0 < min_rr <= 20):
        raise ValueError("strategy policy min_rr must be in (0, 20]")

    if max_notional_usd < 0:
        raise ValueError("strategy policy max_notional_usd must be >= 0")

    if not (0 < risk_pct_multiplier <= 1):
        raise ValueError("strategy policy risk_pct_multiplier must be in (0, 1]")

    if cooldown_minutes < 0:
        raise ValueError("strategy policy cooldown_minutes must be >= 0")

    if max_hold_minutes < 0:
        raise ValueError("strategy policy max_hold_minutes must be >= 0")

    return {
        "min_rr": min_rr,
        "max_notional_usd": max_notional_usd,
        "risk_pct_multiplier": risk_pct_multiplier,
        "cooldown_minutes": cooldown_minutes,
        "max_hold_minutes": max_hold_minutes,
        "allowed_order_types": _ordered_order_types(allowed_order_types),
    }


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
    strategy_policies: Any = None,
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

    try:
        policy = resolve_strategy_execution_policy(
            strategy,
            strategy_policies,
            fallback_min_rr=min_rr,
        )
    except ValueError:
        return _reject(decision, "invalid_strategy_policy")

    if order_type not in policy["allowed_order_types"]:
        return _reject(decision, "order_type_not_allowed")

    entry = _safe_float(plan.get("entry"))
    stop_loss = _safe_float(plan.get("stop_loss"))
    target = _safe_float(plan.get("target"))
    rr = _safe_float(plan.get("rr"))

    if not all(value > 0 for value in (entry, stop_loss, target, rr)):
        return _reject(decision, "invalid_plan_values")

    execution_min_rr = _safe_float(min_rr, 1.2)
    if rr < execution_min_rr:
        return _reject(decision, "rr_below_execution_min")

    if rr < policy["min_rr"]:
        return _reject(decision, "rr_below_strategy_min")

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
        "policy_min_rr": policy["min_rr"],
        "max_notional_usd": policy["max_notional_usd"],
        "risk_pct_multiplier": policy["risk_pct_multiplier"],
        "cooldown_minutes": policy["cooldown_minutes"],
        "max_hold_minutes": policy["max_hold_minutes"],
        "allowed_order_types": policy["allowed_order_types"],
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
