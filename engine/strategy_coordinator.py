from __future__ import annotations

from typing import Any


DECISION_NO_ACTION = "NO_ACTION"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_CONFLICT = "CONFLICT_NO_ACTION"


class ReadOnlyStrategyCoordinator:
    """Chooses one read-only candidate without executing anything."""

    def decide(
        self,
        *,
        symbol: str,
        regime_result: dict[str, Any],
        strategy_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        candidates = [
            result
            for result in strategy_results
            if result.get("status") == DECISION_WATCH_ONLY
            and int(result.get("score", 0) or 0) >= int(result.get("threshold", 0) or 0)
        ]

        if not candidates:
            return {
                "symbol": symbol,
                "decision": DECISION_NO_ACTION,
                "read_only": True,
                "execution_disabled": True,
                "reason": "no_strategy_candidate",
                "regime": regime_result.get("regime"),
                "selected_strategy": None,
            }

        sides = {str(candidate.get("side", "")).upper() for candidate in candidates}
        sides.discard("")
        if len(sides) > 1:
            return {
                "symbol": symbol,
                "decision": DECISION_CONFLICT,
                "read_only": True,
                "execution_disabled": True,
                "reason": "conflicting_strategy_sides",
                "regime": regime_result.get("regime"),
                "selected_strategy": None,
                "candidate_count": len(candidates),
            }

        selected = max(
            candidates,
            key=lambda result: (
                int(result.get("score", 0) or 0),
                float(result.get("rr", 0.0) or 0.0),
                str(result.get("strategy", "")),
            ),
        )
        return {
            "symbol": symbol,
            "decision": DECISION_WATCH_ONLY,
            "read_only": True,
            "execution_disabled": True,
            "reason": "single_read_only_candidate",
            "regime": regime_result.get("regime"),
            "selected_strategy": selected.get("strategy"),
            "side": selected.get("side"),
            "score": selected.get("score"),
            "threshold": selected.get("threshold"),
            "plan": {
                "order_type": selected.get("order_type"),
                "entry": selected.get("entry"),
                "stop_loss": selected.get("stop_loss"),
                "target": selected.get("target"),
                "rr": selected.get("rr"),
            },
        }

