from __future__ import annotations

from typing import Any

from engine.market_regime import (
    REGIME_CHOP,
    REGIME_LOW_VOL_COMPRESSION,
    REGIME_RANGE,
    REGIME_TRENDING,
    safe_float,
)


DECISION_NO_ACTION = "NO_ACTION"
DECISION_WATCH_ONLY = "WATCH_ONLY"
DECISION_CONFLICT = "CONFLICT_NO_ACTION"

REQUIRED_PLAN_FIELDS = ("order_type", "entry", "stop_loss", "target", "rr")
VALID_SIDES = {"LONG", "SHORT"}
STRATEGY_PRIORITY_BY_REGIME = {
    REGIME_RANGE: ("MEAN_REVERSION", "VOLATILITY_EXPANSION", "BREAKOUT", "TREND_PULLBACK"),
    REGIME_LOW_VOL_COMPRESSION: ("BREAKOUT", "VOLATILITY_EXPANSION", "TREND_PULLBACK", "MEAN_REVERSION"),
    REGIME_TRENDING: ("TREND_PULLBACK", "VOLATILITY_EXPANSION", "BREAKOUT", "MEAN_REVERSION"),
    REGIME_CHOP: ("VOLATILITY_EXPANSION", "BREAKOUT", "TREND_PULLBACK", "MEAN_REVERSION"),
}
DEFAULT_STRATEGY_PRIORITY = ("VOLATILITY_EXPANSION", "BREAKOUT", "TREND_PULLBACK", "MEAN_REVERSION")


def _score(result: dict[str, Any]) -> int:
    return round(safe_float(result.get("score")))


def _threshold(result: dict[str, Any]) -> int:
    return round(safe_float(result.get("threshold")))


def _strategy_name(result: dict[str, Any]) -> str:
    return str(result.get("strategy", "UNKNOWN")).upper()


class ReadOnlyStrategyCoordinator:
    """Chooses one read-only candidate without executing anything."""

    def __init__(self, *, min_rr: float = 1.0) -> None:
        self.min_rr = min_rr

    def _strategy_priority(self, regime: str, strategy: str) -> int:
        priorities = STRATEGY_PRIORITY_BY_REGIME.get(regime, DEFAULT_STRATEGY_PRIORITY)
        try:
            return len(priorities) - priorities.index(strategy)
        except ValueError:
            return 0

    def _compact_candidate(
        self,
        result: dict[str, Any],
        *,
        regime: str,
        coordinator_rejection: str | None = None,
    ) -> dict[str, Any]:
        compact = {
            "strategy": _strategy_name(result),
            "status": result.get("status"),
            "side": result.get("side"),
            "score": _score(result),
            "threshold": _threshold(result),
            "rr": safe_float(result.get("rr")),
            "order_type": result.get("order_type"),
            "strategy_priority": self._strategy_priority(regime, _strategy_name(result)),
        }
        reason = str(result.get("reason", "") or "")
        if reason:
            compact["reason"] = reason
        if coordinator_rejection:
            compact["coordinator_rejection"] = coordinator_rejection
        return compact

    def _candidate_rejection(self, result: dict[str, Any]) -> str | None:
        side = str(result.get("side", "")).upper()
        if side not in VALID_SIDES:
            return "invalid_side"

        score = _score(result)
        threshold = _threshold(result)
        if score < threshold:
            return "score_below_threshold"

        missing = [field for field in REQUIRED_PLAN_FIELDS if result.get(field) in (None, "")]
        if missing:
            return "missing_plan_fields:" + ",".join(missing)

        entry = safe_float(result.get("entry"))
        stop_loss = safe_float(result.get("stop_loss"))
        target = safe_float(result.get("target"))
        rr = safe_float(result.get("rr"))
        if entry <= 0 or stop_loss <= 0 or target <= 0:
            return "invalid_plan_prices"
        if rr < self.min_rr:
            return f"coordinator_rr_below_min:{round(rr, 4)}/{self.min_rr}"

        if side == "LONG" and not (stop_loss < entry < target):
            return "invalid_directional_plan"
        if side == "SHORT" and not (target < entry < stop_loss):
            return "invalid_directional_plan"

        return None

    def decide(
        self,
        *,
        symbol: str,
        regime_result: dict[str, Any],
        strategy_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        regime = str(regime_result.get("regime", "UNKNOWN"))
        raw_candidates = [
            result
            for result in strategy_results
            if isinstance(result, dict) and result.get("status") == DECISION_WATCH_ONLY
        ]
        candidates: list[dict[str, Any]] = []
        rejected_candidates: list[dict[str, Any]] = []
        for result in raw_candidates:
            rejection = self._candidate_rejection(result)
            if rejection:
                rejected_candidates.append(
                    self._compact_candidate(result, regime=regime, coordinator_rejection=rejection)
                )
            else:
                candidates.append(result)

        if not candidates:
            return {
                "symbol": symbol,
                "decision": DECISION_NO_ACTION,
                "read_only": True,
                "execution_disabled": True,
                "reason": "no_valid_strategy_candidate" if raw_candidates else "no_strategy_candidate",
                "regime": regime,
                "selected_strategy": None,
                "candidate_count": 0,
                "rejected_candidate_count": len(rejected_candidates),
                "rejected_candidates": rejected_candidates,
            }

        sides = {str(candidate.get("side", "")).upper() for candidate in candidates}
        sides.discard("")
        candidate_strategies = sorted({_strategy_name(candidate) for candidate in candidates})
        compact_candidates = [
            self._compact_candidate(candidate, regime=regime)
            for candidate in sorted(
                candidates,
                key=lambda result: (
                    self._strategy_priority(regime, _strategy_name(result)),
                    _score(result),
                    safe_float(result.get("rr")),
                    _strategy_name(result),
                ),
                reverse=True,
            )
        ]
        if len(sides) > 1:
            return {
                "symbol": symbol,
                "decision": DECISION_CONFLICT,
                "read_only": True,
                "execution_disabled": True,
                "reason": "conflicting_strategy_sides",
                "regime": regime,
                "selected_strategy": None,
                "candidate_count": len(candidates),
                "candidate_sides": sorted(sides),
                "candidate_strategies": candidate_strategies,
                "candidates": compact_candidates,
                "rejected_candidate_count": len(rejected_candidates),
                "rejected_candidates": rejected_candidates,
            }

        selected = max(
            candidates,
            key=lambda result: (
                self._strategy_priority(regime, _strategy_name(result)),
                _score(result),
                safe_float(result.get("rr")),
                _strategy_name(result),
            ),
        )
        reason = "single_read_only_candidate"
        if len(candidates) > 1:
            reason = "selected_highest_priority_candidate"
        elif rejected_candidates:
            reason = "single_valid_read_only_candidate"
        return {
            "symbol": symbol,
            "decision": DECISION_WATCH_ONLY,
            "read_only": True,
            "execution_disabled": True,
            "reason": reason,
            "regime": regime,
            "selected_strategy": selected.get("strategy"),
            "side": selected.get("side"),
            "score": selected.get("score"),
            "threshold": selected.get("threshold"),
            "candidate_count": len(candidates),
            "candidate_strategies": candidate_strategies,
            "strategy_priority": self._strategy_priority(regime, _strategy_name(selected)),
            "rejected_candidate_count": len(rejected_candidates),
            "rejected_candidates": rejected_candidates,
            "candidates": compact_candidates,
            "plan": {
                "order_type": selected.get("order_type"),
                "entry": selected.get("entry"),
                "stop_loss": selected.get("stop_loss"),
                "target": selected.get("target"),
                "rr": selected.get("rr"),
            },
        }
