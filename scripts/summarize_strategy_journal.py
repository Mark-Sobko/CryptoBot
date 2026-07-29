from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _sorted_counter(counter: Counter[str], *, top: int | None = None) -> dict[str, int]:
    items = counter.most_common(top)
    return {key: value for key, value in items}


def load_jsonl(path: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append({"line": line_no, "error": str(exc)})
                continue
            if isinstance(entry, dict):
                entries.append(entry)
            else:
                errors.append({"line": line_no, "error": "entry_is_not_object"})

    return entries, errors


def compact_recent(entry: dict[str, Any]) -> dict[str, Any]:
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    compact = {
        "ts": entry.get("ts"),
        "event_type": entry.get("event_type"),
        "symbol": entry.get("symbol"),
        "regime": data.get("regime"),
        "decision": data.get("decision"),
        "strategy": data.get("selected_strategy") or data.get("strategy"),
        "side": data.get("side"),
        "score": data.get("score"),
        "threshold": data.get("threshold"),
        "reason": data.get("reason"),
        "order_submitted": data.get("order_submitted"),
        "policy_min_rr": data.get("policy_min_rr"),
        "max_notional_usd": data.get("max_notional_usd"),
        "risk_pct_multiplier": data.get("risk_pct_multiplier"),
        "cooldown_minutes": data.get("cooldown_minutes"),
        "max_hold_minutes": data.get("max_hold_minutes"),
    }
    plan = data.get("plan")
    if isinstance(plan, dict):
        compact["plan"] = {
            "order_type": plan.get("order_type"),
            "entry": plan.get("entry"),
            "stop_loss": plan.get("stop_loss"),
            "target": plan.get("target"),
            "rr": plan.get("rr"),
        }
    return {key: value for key, value in compact.items() if value not in (None, "", [])}


def summarize_entries(entries: list[dict[str, Any]], *, top: int = 10) -> dict[str, Any]:
    event_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    strategy_counts: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    regime_counts: Counter[str] = Counter()
    watch_entries: list[dict[str, Any]] = []
    conflict_entries: list[dict[str, Any]] = []
    rejected_entries: list[dict[str, Any]] = []
    execution_entries: list[dict[str, Any]] = []
    execution_submitted_counts: Counter[str] = Counter()
    execution_rejected_counts: Counter[str] = Counter()
    execution_reason_counts: Counter[str] = Counter()
    execution_health_block_counts: Counter[str] = Counter()

    first_ts = ""
    last_ts = ""
    for entry in entries:
        ts = str(entry.get("ts", ""))
        if ts and not first_ts:
            first_ts = ts
        if ts:
            last_ts = ts

        event_type = str(entry.get("event_type", "UNKNOWN"))
        symbol = str(entry.get("symbol", "UNKNOWN"))
        data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        decision = str(data.get("decision", "") or "")
        strategy = str(data.get("selected_strategy") or data.get("strategy") or "")
        reason = str(data.get("reason", "") or "")
        regime = str(data.get("regime", "") or "")

        event_counts[event_type] += 1
        symbol_counts[symbol] += 1
        if decision:
            decision_counts[decision] += 1
        if strategy:
            strategy_counts[strategy] += 1
        if reason:
            reason_counts[reason] += 1
        if regime:
            regime_counts[regime] += 1

        if decision == "WATCH_ONLY":
            watch_entries.append(compact_recent(entry))
        elif decision == "CONFLICT_NO_ACTION":
            conflict_entries.append(compact_recent(entry))
        elif int(data.get("rejected_candidate_count", 0) or 0) > 0:
            rejected_entries.append(compact_recent(entry))

        if event_type == "ALT_STRATEGY_EXECUTION":
            execution_entries.append(compact_recent(entry))
            execution_strategy = strategy or "UNKNOWN"
            if bool(data.get("order_submitted", False)):
                execution_submitted_counts[execution_strategy] += 1
            else:
                execution_rejected_counts[execution_strategy] += 1
            if reason:
                execution_reason_counts[reason] += 1
                if reason.startswith("strategy_health_guard:"):
                    execution_health_block_counts[execution_strategy] += 1

    return {
        "entries_total": len(entries),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "event_counts": _sorted_counter(event_counts, top=top),
        "decision_counts": _sorted_counter(decision_counts, top=top),
        "strategy_counts": _sorted_counter(strategy_counts, top=top),
        "symbol_counts": _sorted_counter(symbol_counts, top=top),
        "regime_counts": _sorted_counter(regime_counts, top=top),
        "reason_counts": _sorted_counter(reason_counts, top=top),
        "watch_total": len(watch_entries),
        "conflict_total": len(conflict_entries),
        "rejected_candidate_total": len(rejected_entries),
        "execution_total": len(execution_entries),
        "execution_submitted_counts": _sorted_counter(execution_submitted_counts, top=top),
        "execution_rejected_counts": _sorted_counter(execution_rejected_counts, top=top),
        "execution_reason_counts": _sorted_counter(execution_reason_counts, top=top),
        "execution_health_block_counts": _sorted_counter(execution_health_block_counts, top=top),
        "recent_watch": watch_entries[-top:],
        "recent_conflicts": conflict_entries[-top:],
        "recent_rejections": rejected_entries[-top:],
        "recent_executions": execution_entries[-top:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize CryptoBot strategy observation JSONL logs."
    )
    parser.add_argument("path", help="Path to strategy_observations.jsonl")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    entries, errors = load_jsonl(args.path)
    summary = summarize_entries(entries, top=max(args.top, 1))
    summary["parse_errors_total"] = len(errors)
    summary["parse_errors"] = errors[-10:]
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
