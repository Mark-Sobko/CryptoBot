from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any


HEALTH_OK = "OK"
HEALTH_BLOCKED = "BLOCKED"

BENIGN_REJECTION_REASONS = {
    "",
    "ready",
    "submitted",
    "strategy_cooldown",
    "max_positions_reached",
}

UNHEALTHY_REASON_PREFIXES = (
    "executor_failed",
    "risk_reject:",
    "zero_qty_after_risk_sizing",
    "zero_qty_after_notional_cap",
)


def _normalize_strategy(value: Any) -> str:
    return str(value or "").strip().upper()


def _event_time(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        timestamp = value
    elif isinstance(value, str) and value.strip():
        try:
            timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=dt.timezone.utc)
    return timestamp.astimezone(dt.timezone.utc)


def is_unhealthy_rejection(reason: Any) -> bool:
    normalized = str(reason or "").strip()
    if normalized in BENIGN_REJECTION_REASONS:
        return False
    if normalized.startswith("strategy_health_guard"):
        return False
    return normalized.startswith(UNHEALTHY_REASON_PREFIXES)


def evaluate_strategy_execution_health(
    events: Iterable[Mapping[str, Any]],
    *,
    strategy: Any,
    now: dt.datetime | None = None,
    window_minutes: float = 240.0,
    max_recent_rejections: int = 3,
    max_executor_failures: int = 1,
) -> dict[str, Any]:
    strategy_name = _normalize_strategy(strategy)
    if not strategy_name:
        return {"status": HEALTH_OK, "reason": "", "strategy": strategy_name}

    current_time = now or dt.datetime.now(dt.timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=dt.timezone.utc)
    current_time = current_time.astimezone(dt.timezone.utc)

    max_age_seconds = max(float(window_minutes or 0.0), 0.0) * 60.0
    recent: list[dict[str, Any]] = []
    for event in events:
        if _normalize_strategy(event.get("strategy")) != strategy_name:
            continue

        timestamp = _event_time(event.get("ts"))
        if timestamp is None:
            continue
        age_seconds = (current_time - timestamp).total_seconds()
        if max_age_seconds > 0 and age_seconds > max_age_seconds:
            continue

        reason = str(event.get("reason", "") or "")
        order_submitted = bool(event.get("order_submitted", False))
        recent.append(
            {
                "ts": timestamp,
                "symbol": str(event.get("symbol", "") or "").upper(),
                "strategy": strategy_name,
                "reason": reason,
                "order_submitted": order_submitted,
                "unhealthy_rejection": (not order_submitted) and is_unhealthy_rejection(reason),
            }
        )

    recent.sort(key=lambda item: item["ts"])

    executor_failures = sum(
        1 for event in recent if str(event["reason"]).startswith("executor_failed")
    )
    consecutive_unhealthy_rejections = 0
    for event in reversed(recent):
        if event["order_submitted"]:
            break
        if not event["unhealthy_rejection"]:
            break
        consecutive_unhealthy_rejections += 1

    if max_executor_failures > 0 and executor_failures >= max_executor_failures:
        return {
            "status": HEALTH_BLOCKED,
            "reason": "strategy_health_guard:executor_failures",
            "strategy": strategy_name,
            "recent_events": len(recent),
            "executor_failures": executor_failures,
            "consecutive_unhealthy_rejections": consecutive_unhealthy_rejections,
        }

    if (
        max_recent_rejections > 0
        and consecutive_unhealthy_rejections >= max_recent_rejections
    ):
        return {
            "status": HEALTH_BLOCKED,
            "reason": "strategy_health_guard:rejection_streak",
            "strategy": strategy_name,
            "recent_events": len(recent),
            "executor_failures": executor_failures,
            "consecutive_unhealthy_rejections": consecutive_unhealthy_rejections,
        }

    return {
        "status": HEALTH_OK,
        "reason": "",
        "strategy": strategy_name,
        "recent_events": len(recent),
        "executor_failures": executor_failures,
        "consecutive_unhealthy_rejections": consecutive_unhealthy_rejections,
    }
