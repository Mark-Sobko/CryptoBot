#!/usr/bin/env python3
"""Summarize market regime observer JSON or JSONL output."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


WATCH_STATUSES = {"RANGE_EDGE_WATCH", "WAIT_BREAKOUT"}
WATCH_ONLY = "WATCH_ONLY"
STRATEGY_KEYS = (
    "mean_reversion",
    "breakout",
    "trend_pullback",
    "volatility_expansion",
)
NON_ACTIONABLE_STRATEGY_STATUSES = {"DISABLED", "UNKNOWN", "None", ""}


def _dicts(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def load_observer_payload(path: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"empty observer output: {source}")

    if text.startswith("{"):
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError(f"observer JSON must be an object: {source}")
        if isinstance(payload.get("result"), dict) and payload.get("type") == "cycle":
            return progress_records_to_payload([payload], source=str(source))
        payload.setdefault("source", str(source))
        return payload

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f"JSONL line {line_number} is not an object: {source}")
        records.append(record)

    return progress_records_to_payload(records, source=str(source))


def progress_records_to_payload(records: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    cycles = [
        record["result"]
        for record in records
        if isinstance(record.get("result"), dict)
    ]
    last_record = records[-1] if records else {}
    return {
        "status": "PROGRESS_JSONL",
        "source": source,
        "cycles_requested": last_record.get("cycles_requested", len(cycles)),
        "cycles_completed": len(cycles),
        "cycles": cycles,
    }


def iter_results(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for cycle in cycles:
        results.extend(_dicts(cycle.get("results")))
    return results


def compact_watch(result: dict[str, Any], strategy_key: str | None = None) -> dict[str, Any]:
    compact = {
        "symbol": result.get("symbol"),
        "regime": result.get("regime"),
        "status": result.get("status"),
        "confidence": result.get("confidence"),
        "trade_posture": result.get("trade_posture"),
        "reason": result.get("reason"),
    }
    if strategy_key:
        strategy = result.get(strategy_key)
        if isinstance(strategy, dict):
            compact["strategy"] = strategy.get("strategy")
            compact["strategy_status"] = strategy.get("status")
            compact["side"] = strategy.get("side")
            compact["score"] = strategy.get("score")
            compact["threshold"] = strategy.get("threshold")
            compact["failed_checks"] = strategy.get("failed_checks", [])
            compact["plan"] = strategy.get("plan")
            compact["strategy_reason"] = strategy.get("reason")
    decision = result.get("decision")
    if isinstance(decision, dict):
        compact["decision"] = {
            "decision": decision.get("decision"),
            "selected_strategy": decision.get("selected_strategy"),
            "side": decision.get("side"),
            "score": decision.get("score"),
            "reason": decision.get("reason"),
            "plan": decision.get("plan"),
        }
    return compact


def _sorted_counter(counter: Counter) -> dict[str, int]:
    return {
        str(key): int(value)
        for key, value in sorted(counter.items(), key=lambda item: (str(item[0]), item[1]))
    }


def summarize_strategy_blockers(results: list[dict[str, Any]], *, top: int = 10) -> dict[str, Any]:
    failed_checks: dict[str, Counter] = {strategy: Counter() for strategy in STRATEGY_KEYS}
    reasons: dict[str, Counter] = {strategy: Counter() for strategy in STRATEGY_KEYS}
    actionable_symbol_statuses: Counter[tuple[str, str, str]] = Counter()
    regime_symbol_statuses: Counter[tuple[str, str, str]] = Counter()

    for result in results:
        symbol = str(result.get("symbol", "UNKNOWN"))
        regime_symbol_statuses[
            (
                symbol,
                str(result.get("regime", "UNKNOWN")),
                str(result.get("status", "UNKNOWN")),
            )
        ] += 1

        for strategy_key in STRATEGY_KEYS:
            strategy = result.get(strategy_key)
            if not isinstance(strategy, dict):
                continue

            status = str(strategy.get("status", "UNKNOWN"))
            reason = str(strategy.get("reason", ""))
            if reason and status not in NON_ACTIONABLE_STRATEGY_STATUSES:
                reasons[strategy_key][reason] += 1

            failed = strategy.get("failed_checks")
            if isinstance(failed, list) and status not in NON_ACTIONABLE_STRATEGY_STATUSES:
                failed_checks[strategy_key].update(str(item) for item in failed)

            if status not in NON_ACTIONABLE_STRATEGY_STATUSES:
                actionable_symbol_statuses[(strategy_key, symbol, status)] += 1

    return {
        "strategy_failed_check_counts": {
            strategy: _sorted_counter(counter)
            for strategy, counter in failed_checks.items()
        },
        "strategy_reason_counts": {
            strategy: _sorted_counter(counter)
            for strategy, counter in reasons.items()
        },
        "top_strategy_symbol_statuses": [
            {
                "strategy": strategy,
                "symbol": symbol,
                "status": status,
                "count": int(count),
            }
            for (strategy, symbol, status), count in actionable_symbol_statuses.most_common(top)
        ],
        "top_regime_symbol_statuses": [
            {
                "symbol": symbol,
                "regime": regime,
                "status": status,
                "count": int(count),
            }
            for (symbol, regime, status), count in regime_symbol_statuses.most_common(top)
        ],
    }


def summarize_payload(payload: dict[str, Any], *, top: int = 10) -> dict[str, Any]:
    cycles = _dicts(payload.get("cycles"))
    results = iter_results(cycles)

    regime_counts = Counter(str(result.get("regime", "UNKNOWN")) for result in results)
    posture_counts = Counter(str(result.get("trade_posture", "UNKNOWN")) for result in results)
    status_counts = Counter(str(result.get("status", "UNKNOWN")) for result in results)
    mean_reversion_counts = Counter(
        str((result.get("mean_reversion") or {}).get("status", "UNKNOWN"))
        for result in results
    )
    breakout_counts = Counter(
        str((result.get("breakout") or {}).get("status", "UNKNOWN"))
        for result in results
    )
    trend_pullback_counts = Counter(
        str((result.get("trend_pullback") or {}).get("status", "UNKNOWN"))
        for result in results
    )
    volatility_expansion_counts = Counter(
        str((result.get("volatility_expansion") or {}).get("status", "UNKNOWN"))
        for result in results
    )
    decision_counts = Counter(
        str((result.get("decision") or {}).get("decision", "UNKNOWN"))
        for result in results
    )

    regime_watches = [
        compact_watch(result)
        for result in results
        if str(result.get("status")) in WATCH_STATUSES
    ]
    mean_reversion_watches = [
        compact_watch(result, "mean_reversion")
        for result in results
        if (result.get("mean_reversion") or {}).get("status") == WATCH_ONLY
    ]
    breakout_watches = [
        compact_watch(result, "breakout")
        for result in results
        if (result.get("breakout") or {}).get("status") == WATCH_ONLY
    ]
    trend_pullback_watches = [
        compact_watch(result, "trend_pullback")
        for result in results
        if (result.get("trend_pullback") or {}).get("status") == WATCH_ONLY
    ]
    volatility_expansion_watches = [
        compact_watch(result, "volatility_expansion")
        for result in results
        if (result.get("volatility_expansion") or {}).get("status") == WATCH_ONLY
    ]
    coordinator_watches = [
        compact_watch(result)
        for result in results
        if (result.get("decision") or {}).get("decision") == WATCH_ONLY
    ]
    cycle_errors = [
        cycle.get("cycle_error")
        for cycle in cycles
        if isinstance(cycle.get("cycle_error"), dict)
    ]

    summary = {
        "source": payload.get("source"),
        "status": payload.get("status"),
        "cycles_requested": payload.get("cycles_requested"),
        "cycles_completed": payload.get("cycles_completed", len(cycles)),
        "symbols": payload.get("symbols"),
        "symbols_scanned_total": sum(int(cycle.get("symbols_scanned", 0) or 0) for cycle in cycles),
        "regime_counts": dict(sorted(regime_counts.items())),
        "trade_posture_counts": dict(sorted(posture_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "mean_reversion_status_counts": dict(sorted(mean_reversion_counts.items())),
        "breakout_status_counts": dict(sorted(breakout_counts.items())),
        "trend_pullback_status_counts": dict(sorted(trend_pullback_counts.items())),
        "volatility_expansion_status_counts": dict(sorted(volatility_expansion_counts.items())),
        "coordinator_decision_counts": dict(sorted(decision_counts.items())),
        "watch_total": len(regime_watches),
        "mean_reversion_watch_total": len(mean_reversion_watches),
        "breakout_watch_total": len(breakout_watches),
        "trend_pullback_watch_total": len(trend_pullback_watches),
        "volatility_expansion_watch_total": len(volatility_expansion_watches),
        "coordinator_watch_total": len(coordinator_watches),
        "cycle_errors_total": len(cycle_errors),
        "cycle_errors": cycle_errors[-top:],
        "top_regime_watches": regime_watches[:top],
        "top_mean_reversion_watches": mean_reversion_watches[:top],
        "top_breakout_watches": breakout_watches[:top],
        "top_trend_pullback_watches": trend_pullback_watches[:top],
        "top_volatility_expansion_watches": volatility_expansion_watches[:top],
        "top_coordinator_watches": coordinator_watches[:top],
        **summarize_strategy_blockers(results, top=top),
    }
    summary["recommendation"] = build_recommendation(summary)
    return summary


def build_recommendation(summary: dict[str, Any]) -> dict[str, str]:
    cycles_completed = int(summary.get("cycles_completed", 0) or 0)
    cycle_errors_total = int(summary.get("cycle_errors_total", 0) or 0)
    coordinator_watch_total = int(summary.get("coordinator_watch_total", 0) or 0)
    strategy_watch_total = int(summary.get("mean_reversion_watch_total", 0) or 0) + int(
        summary.get("breakout_watch_total", 0) or 0
    ) + int(summary.get("trend_pullback_watch_total", 0) or 0) + int(
        summary.get("volatility_expansion_watch_total", 0) or 0
    )

    if cycles_completed <= 0:
        return {
            "status": "NO_DATA",
            "next_step": "run_observer",
            "reason": "no_completed_cycles",
        }
    if cycle_errors_total >= cycles_completed:
        return {
            "status": "DATA_COLLECTION_BROKEN",
            "next_step": "fix_connectivity_or_exchange_errors",
            "reason": "all_cycles_failed",
        }
    if coordinator_watch_total > 0:
        return {
            "status": "REVIEW_COORDINATED_WATCHES",
            "next_step": "inspect_watch_candidates_before_demo_execution",
            "reason": "coordinator_found_read_only_candidates",
        }
    if strategy_watch_total > 0:
        return {
            "status": "REVIEW_STRATEGY_WATCHES",
            "next_step": "inspect_strategy_candidates_and_conflicts",
            "reason": "strategy_found_read_only_candidates",
        }
    if cycles_completed < 24:
        return {
            "status": "KEEP_OBSERVING",
            "next_step": "collect_more_cycles",
            "reason": "sample_is_still_small",
        }
    return {
        "status": "NO_ALT_STRATEGY_CANDIDATES_YET",
        "next_step": "keep_smc_only_and_continue_observation",
        "reason": "no_read_only_candidates_after_observation_window",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Observer final/checkpoint JSON or progress JSONL path.")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    payload = load_observer_payload(args.path)
    summary = summarize_payload(payload, top=max(args.top, 1))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
