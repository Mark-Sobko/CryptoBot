#!/usr/bin/env python3
"""Read-only market regime observer.

This script does not import TradeExecutor and never places orders. It classifies
symbols into TRENDING, RANGE, LOW_VOL_COMPRESSION, CHOP, or DATA_ERROR so new
non-SMC regimes can be evaluated before any execution logic is considered.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from core.exchange import ExchangeManager
from engine.market_regime import (
    REGIME_CHOP,
    REGIME_DATA_ERROR,
    REGIME_LOW_VOL_COMPRESSION,
    REGIME_RANGE,
    REGIME_TRENDING,
    MarketRegimeClassifier,
)
from engine.strategies.breakout import STATUS_WATCH_ONLY as BO_STATUS_WATCH_ONLY
from engine.strategies.breakout import BreakoutStrategy
from engine.strategies.mean_reversion import STATUS_WATCH_ONLY as MR_STATUS_WATCH_ONLY
from engine.strategies.mean_reversion import MeanReversionStrategy
from engine.strategies.trend_pullback import STATUS_WATCH_ONLY as TP_STATUS_WATCH_ONLY
from engine.strategies.trend_pullback import TrendPullbackStrategy
from engine.strategies.volatility_expansion import STATUS_WATCH_ONLY as VE_STATUS_WATCH_ONLY
from engine.strategies.volatility_expansion import VolatilityExpansionStrategy
from engine.strategy_coordinator import ReadOnlyStrategyCoordinator
from scripts.run_strategy_observer import (
    parse_symbols,
    validate_market_data,
    validate_read_only_environment,
)


WATCH_STATUSES = {
    "RANGE_EDGE_WATCH",
    "WAIT_BREAKOUT",
}
STRATEGY_KEYS = (
    "mean_reversion",
    "breakout",
    "trend_pullback",
    "volatility_expansion",
)
NON_ACTIONABLE_STRATEGY_STATUSES = {"DISABLED", "UNKNOWN", "None", ""}
RUN_STATUS_OK = "OK"
RUN_STATUS_COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
RUN_STATUS_INTERRUPTED = "INTERRUPTED"
RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_RUNNING_WITH_ERRORS = "RUNNING_WITH_ERRORS"
STATUS_CYCLE_ERROR = "CYCLE_ERROR"


def compact_mean_reversion(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None

    compact = {
        "strategy": result.get("strategy"),
        "status": result.get("status"),
        "score": result.get("score"),
        "threshold": result.get("threshold"),
        "side": result.get("side"),
        "reason": result.get("reason"),
        "failed_checks": result.get("failed_checks", []),
        "read_only": True,
        "execution_disabled": True,
    }

    if result.get("status") == MR_STATUS_WATCH_ONLY:
        compact["plan"] = {
            "order_type": result.get("order_type"),
            "entry": result.get("entry"),
            "stop_loss": result.get("stop_loss"),
            "target": result.get("target"),
            "rr": result.get("rr"),
        }

    checks = result.get("checks")
    if isinstance(checks, dict):
        compact["checks"] = {
            "volume_ratio": checks.get("volume_ratio"),
            "max_trigger_volume_ratio": checks.get("max_trigger_volume_ratio"),
            "min_rr": checks.get("min_rr"),
            "touched_edge": checks.get("touched_edge"),
            "reclaimed_inside": checks.get("reclaimed_inside"),
            "edge_rejection": checks.get("edge_rejection"),
        }

    return compact


def compact_breakout(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None

    compact = {
        "strategy": result.get("strategy"),
        "status": result.get("status"),
        "score": result.get("score"),
        "threshold": result.get("threshold"),
        "side": result.get("side"),
        "reason": result.get("reason"),
        "failed_checks": result.get("failed_checks", []),
        "read_only": True,
        "execution_disabled": True,
    }

    if result.get("status") == BO_STATUS_WATCH_ONLY:
        compact["plan"] = {
            "order_type": result.get("order_type"),
            "entry": result.get("entry"),
            "stop_loss": result.get("stop_loss"),
            "target": result.get("target"),
            "rr": result.get("rr"),
        }

    checks = result.get("checks")
    if isinstance(checks, dict):
        compact["checks"] = {
            "breakout_volume_ratio": checks.get("breakout_volume_ratio"),
            "min_volume_ratio": checks.get("min_volume_ratio"),
            "breakout_body_ratio": checks.get("breakout_body_ratio"),
            "min_body_ratio": checks.get("min_body_ratio"),
            "extension_range_pct": checks.get("extension_range_pct"),
            "max_extension_range_pct": checks.get("max_extension_range_pct"),
            "retest_confirmed": checks.get("retest_confirmed"),
            "held_beyond_edge": checks.get("held_beyond_edge"),
            "min_rr": checks.get("min_rr"),
        }

    return compact


def compact_trend_pullback(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None

    compact = {
        "strategy": result.get("strategy"),
        "status": result.get("status"),
        "score": result.get("score"),
        "threshold": result.get("threshold"),
        "side": result.get("side"),
        "reason": result.get("reason"),
        "failed_checks": result.get("failed_checks", []),
        "read_only": True,
        "execution_disabled": True,
    }

    if result.get("status") == TP_STATUS_WATCH_ONLY:
        compact["plan"] = {
            "order_type": result.get("order_type"),
            "entry": result.get("entry"),
            "stop_loss": result.get("stop_loss"),
            "target": result.get("target"),
            "rr": result.get("rr"),
        }

    checks = result.get("checks")
    if isinstance(checks, dict):
        compact["checks"] = {
            "htf_ema50_slope_pct": checks.get("htf_ema50_slope_pct"),
            "ltf_aligned": checks.get("ltf_aligned"),
            "touched_value": checks.get("touched_value"),
            "reclaimed_value": checks.get("reclaimed_value"),
            "structure_held": checks.get("structure_held"),
            "value_distance_atr": checks.get("value_distance_atr"),
            "max_value_distance_atr": checks.get("max_value_distance_atr"),
            "pullback_volume_ratio": checks.get("pullback_volume_ratio"),
            "max_pullback_volume_ratio": checks.get("max_pullback_volume_ratio"),
            "trigger_body_ratio": checks.get("trigger_body_ratio"),
            "trigger_volume_ratio": checks.get("trigger_volume_ratio"),
            "limit_entry": checks.get("limit_entry"),
            "min_rr": checks.get("min_rr"),
        }

    return compact


def compact_volatility_expansion(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None

    compact = {
        "strategy": result.get("strategy"),
        "status": result.get("status"),
        "score": result.get("score"),
        "threshold": result.get("threshold"),
        "side": result.get("side"),
        "reason": result.get("reason"),
        "failed_checks": result.get("failed_checks", []),
        "read_only": True,
        "execution_disabled": True,
    }

    if result.get("status") == VE_STATUS_WATCH_ONLY:
        compact["plan"] = {
            "order_type": result.get("order_type"),
            "entry": result.get("entry"),
            "stop_loss": result.get("stop_loss"),
            "target": result.get("target"),
            "rr": result.get("rr"),
        }

    checks = result.get("checks")
    if isinstance(checks, dict):
        compact["checks"] = {
            "atr_expansion_ratio": checks.get("atr_expansion_ratio"),
            "min_atr_expansion_ratio": checks.get("min_atr_expansion_ratio"),
            "volume_ratio": checks.get("volume_ratio"),
            "min_volume_ratio": checks.get("min_volume_ratio"),
            "body_ratio": checks.get("body_ratio"),
            "min_body_ratio": checks.get("min_body_ratio"),
            "extension_range_pct": checks.get("extension_range_pct"),
            "max_extension_range_pct": checks.get("max_extension_range_pct"),
            "extension_atr": checks.get("extension_atr"),
            "max_extension_atr": checks.get("max_extension_atr"),
            "htf_not_opposed": checks.get("htf_not_opposed"),
            "held_beyond_edge": checks.get("held_beyond_edge"),
            "continuation_direction": checks.get("continuation_direction"),
            "continuation_body": checks.get("continuation_body"),
            "five_volume_ratio": checks.get("five_volume_ratio"),
            "min_rr": checks.get("min_rr"),
        }

    return compact


def compact_decision(decision: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(decision, dict):
        return None

    compact = {
        "decision": decision.get("decision"),
        "selected_strategy": decision.get("selected_strategy"),
        "side": decision.get("side"),
        "score": decision.get("score"),
        "threshold": decision.get("threshold"),
        "reason": decision.get("reason"),
        "read_only": True,
        "execution_disabled": True,
    }
    if isinstance(decision.get("plan"), dict):
        compact["plan"] = decision["plan"]
    return compact


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "symbol": result.get("symbol"),
        "status": result.get("status"),
        "regime": result.get("regime"),
        "confidence": result.get("confidence"),
        "trade_posture": result.get("trade_posture"),
        "reason": result.get("reason"),
    }

    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        compact["metrics"] = {
            "adx": metrics.get("adx"),
            "atr_pct": metrics.get("atr_pct"),
            "atr_percentile": metrics.get("atr_percentile"),
            "bb_width_percentile": metrics.get("bb_width_percentile"),
            "efficiency_ratio": metrics.get("efficiency_ratio"),
            "relative_volume": metrics.get("relative_volume"),
            "range_width_pct": metrics.get("range_width_pct"),
            "range_position": metrics.get("range_position"),
            "upper_touches": metrics.get("upper_touches"),
            "lower_touches": metrics.get("lower_touches"),
        }

    setup = result.get("setup")
    if isinstance(setup, dict):
        compact["setup"] = setup

    mean_reversion = compact_mean_reversion(result.get("mean_reversion"))
    if mean_reversion:
        compact["mean_reversion"] = mean_reversion

    breakout = compact_breakout(result.get("breakout"))
    if breakout:
        compact["breakout"] = breakout

    trend_pullback = compact_trend_pullback(result.get("trend_pullback"))
    if trend_pullback:
        compact["trend_pullback"] = trend_pullback

    volatility_expansion = compact_volatility_expansion(result.get("volatility_expansion"))
    if volatility_expansion:
        compact["volatility_expansion"] = volatility_expansion

    decision = compact_decision(result.get("decision"))
    if decision:
        compact["decision"] = decision

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


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    regime_counts = Counter(str(result.get("regime", "UNKNOWN")) for result in results)
    posture_counts = Counter(str(result.get("trade_posture", "UNKNOWN")) for result in results)
    status_counts = Counter(str(result.get("status", "UNKNOWN")) for result in results)
    mean_reversion_status_counts = Counter(
        str((result.get("mean_reversion") or {}).get("status", "UNKNOWN"))
        for result in results
    )
    breakout_status_counts = Counter(
        str((result.get("breakout") or {}).get("status", "UNKNOWN"))
        for result in results
    )
    trend_pullback_status_counts = Counter(
        str((result.get("trend_pullback") or {}).get("status", "UNKNOWN"))
        for result in results
    )
    volatility_expansion_status_counts = Counter(
        str((result.get("volatility_expansion") or {}).get("status", "UNKNOWN"))
        for result in results
    )
    coordinator_decision_counts = Counter(
        str((result.get("decision") or {}).get("decision", "UNKNOWN"))
        for result in results
    )

    watches = [
        compact_result(result)
        for result in results
        if str(result.get("status")) in WATCH_STATUSES
    ]
    range_watches = [item for item in watches if item.get("regime") == REGIME_RANGE]
    compression_watches = [
        item for item in watches if item.get("regime") == REGIME_LOW_VOL_COMPRESSION
    ]
    mean_reversion_watches = [
        compact_result(result)
        for result in results
        if (result.get("mean_reversion") or {}).get("status") == MR_STATUS_WATCH_ONLY
    ]
    breakout_watches = [
        compact_result(result)
        for result in results
        if (result.get("breakout") or {}).get("status") == BO_STATUS_WATCH_ONLY
    ]
    trend_pullback_watches = [
        compact_result(result)
        for result in results
        if (result.get("trend_pullback") or {}).get("status") == TP_STATUS_WATCH_ONLY
    ]
    volatility_expansion_watches = [
        compact_result(result)
        for result in results
        if (result.get("volatility_expansion") or {}).get("status") == VE_STATUS_WATCH_ONLY
    ]

    high_confidence = [
        compact_result(result)
        for result in sorted(
            results,
            key=lambda item: (
                int(item.get("confidence", 0) or 0),
                str(item.get("symbol", "")),
            ),
            reverse=True,
        )[:10]
    ]

    return {
        "regime_counts": dict(sorted(regime_counts.items())),
        "trade_posture_counts": dict(sorted(posture_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "watch_total": len(watches),
        "range_edge_watch_total": len(range_watches),
        "compression_watch_total": len(compression_watches),
        "mean_reversion_watch_total": len(mean_reversion_watches),
        "breakout_watch_total": len(breakout_watches),
        "trend_pullback_watch_total": len(trend_pullback_watches),
        "volatility_expansion_watch_total": len(volatility_expansion_watches),
        "range_edge_watches": range_watches[:10],
        "compression_watches": compression_watches[:10],
        "mean_reversion_watches": mean_reversion_watches[:10],
        "breakout_watches": breakout_watches[:10],
        "trend_pullback_watches": trend_pullback_watches[:10],
        "volatility_expansion_watches": volatility_expansion_watches[:10],
        "mean_reversion_status_counts": dict(sorted(mean_reversion_status_counts.items())),
        "breakout_status_counts": dict(sorted(breakout_status_counts.items())),
        "trend_pullback_status_counts": dict(sorted(trend_pullback_status_counts.items())),
        "volatility_expansion_status_counts": dict(sorted(volatility_expansion_status_counts.items())),
        "coordinator_decision_counts": dict(sorted(coordinator_decision_counts.items())),
        "high_confidence": high_confidence,
        **summarize_strategy_blockers(results, top=10),
    }


def summarize_cycles(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    all_results: list[dict[str, Any]] = []
    for cycle in cycles:
        results = cycle.get("results")
        if isinstance(results, list):
            all_results.extend(result for result in results if isinstance(result, dict))

    cycle_errors = [
        cycle.get("cycle_error")
        for cycle in cycles
        if isinstance(cycle.get("cycle_error"), dict)
    ]
    summary = summarize_results(all_results)
    summary["cycles_with_watch"] = sum(
        1
        for cycle in cycles
        if (cycle.get("summary") or {}).get("watch_total", 0)
    )
    summary["cycles_with_mean_reversion_watch"] = sum(
        1
        for cycle in cycles
        if (cycle.get("summary") or {}).get("mean_reversion_watch_total", 0)
    )
    summary["cycles_with_breakout_watch"] = sum(
        1
        for cycle in cycles
        if (cycle.get("summary") or {}).get("breakout_watch_total", 0)
    )
    summary["cycles_with_trend_pullback_watch"] = sum(
        1
        for cycle in cycles
        if (cycle.get("summary") or {}).get("trend_pullback_watch_total", 0)
    )
    summary["cycles_with_volatility_expansion_watch"] = sum(
        1
        for cycle in cycles
        if (cycle.get("summary") or {}).get("volatility_expansion_watch_total", 0)
    )
    summary["errors_total"] = sum(
        1 for result in all_results if result.get("regime") == REGIME_DATA_ERROR
    )
    summary["cycle_errors_total"] = len(cycle_errors)
    summary["cycle_errors"] = cycle_errors[-10:]
    return summary


def write_json_file(path: str, payload: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_name(f".{target.name}.tmp")
    temp_target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_target.replace(target)


def append_jsonl_file(path: str, payload: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()


def _record_output_error(
    output_errors: list[dict[str, Any]],
    *,
    path: str,
    operation: str,
    exc: Exception,
) -> None:
    error = {
        "path": path,
        "operation": operation,
        "error": f"{type(exc).__name__}:{str(exc)[:180]}",
    }
    output_errors.append(error)
    print(
        f"OUTPUT_WRITE_ERROR {operation} {path}: {error['error']}",
        file=sys.stderr,
    )


def safe_write_json_file(
    path: str,
    payload: dict[str, Any],
    output_errors: list[dict[str, Any]],
) -> None:
    try:
        write_json_file(path, payload)
    except Exception as exc:
        _record_output_error(
            output_errors,
            path=path,
            operation="write_json",
            exc=exc,
        )


def safe_append_jsonl_file(
    path: str,
    payload: dict[str, Any],
    output_errors: list[dict[str, Any]],
) -> None:
    try:
        append_jsonl_file(path, payload)
    except Exception as exc:
        _record_output_error(
            output_errors,
            path=path,
            operation="append_jsonl",
            exc=exc,
        )


def build_cycle_error(symbols: list[str], exc: Exception) -> dict[str, Any]:
    reason = f"{type(exc).__name__}:{str(exc)[:180]}"
    results = [
        {
            "symbol": symbol,
            "status": "ERROR",
            "regime": REGIME_DATA_ERROR,
            "confidence": 0,
            "trade_posture": "NO_TRADE",
            "reason": reason,
            "metrics": {"ok": False, "reason": "cycle_exception"},
        }
        for symbol in symbols
    ]
    return {
        "status": STATUS_CYCLE_ERROR,
        "read_only": True,
        "execution_disabled": True,
        "symbols_scanned": 0,
        "symbols_requested": len(symbols),
        "cycle_error": {
            "reason": reason,
            "symbols_requested": len(symbols),
        },
        "results": results,
        "summary": summarize_results(results),
    }


def build_run_output(
    *,
    status: str,
    cycles_requested: int,
    symbols: list[str],
    cycles: list[dict[str, Any]],
    summary_only: bool,
    output_errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = summarize_cycles(cycles)
    if output_errors:
        summary["output_errors_total"] = len(output_errors)
        summary["output_errors"] = output_errors[-10:]

    return {
        "status": status,
        "read_only": True,
        "execution_disabled": True,
        "environment": {"demo": config.BYBIT_DEMO, "testnet": config.BYBIT_TESTNET},
        "cycles_requested": cycles_requested,
        "cycles_completed": len(cycles),
        "symbols": symbols,
        "summary": summary,
        "cycles": [compact_cycle(cycle) for cycle in cycles] if summary_only else cycles,
    }


class ReadOnlyMarketRegimeObserver:
    def __init__(self) -> None:
        self.exchange = ExchangeManager()
        self.classifier = MarketRegimeClassifier()
        self.mean_reversion = MeanReversionStrategy()
        self.breakout = BreakoutStrategy()
        self.trend_pullback = TrendPullbackStrategy()
        self.volatility_expansion = VolatilityExpansionStrategy()
        self.coordinator = ReadOnlyStrategyCoordinator()

    def run_cycle(self, symbols: list[str]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []

        for symbol in symbols:
            try:
                data = self.exchange.fetch_all_timeframes(symbol)
                ok, reason = validate_market_data(data)
                if not ok:
                    results.append(
                        {
                            "symbol": symbol,
                            "status": "ERROR",
                            "regime": REGIME_DATA_ERROR,
                            "confidence": 0,
                            "trade_posture": "NO_TRADE",
                            "reason": reason,
                            "metrics": {"ok": False, "reason": reason},
                        }
                    )
                    continue

                analyzed = self.classifier.analyze(data)
                mean_reversion = self.mean_reversion.analyze(
                    symbol=symbol,
                    regime_result=analyzed,
                    df_15m=data.get("15m"),
                    df_5m=data.get("5m"),
                )
                breakout = self.breakout.analyze(
                    symbol=symbol,
                    regime_result=analyzed,
                    df_15m=data.get("15m"),
                    df_5m=data.get("5m"),
                )
                trend_pullback = self.trend_pullback.analyze(
                    symbol=symbol,
                    regime_result=analyzed,
                    df_1h=data.get("1h"),
                    df_15m=data.get("15m"),
                    df_5m=data.get("5m"),
                )
                volatility_expansion = self.volatility_expansion.analyze(
                    symbol=symbol,
                    regime_result=analyzed,
                    df_1h=data.get("1h"),
                    df_15m=data.get("15m"),
                    df_5m=data.get("5m"),
                )
                decision = self.coordinator.decide(
                    symbol=symbol,
                    regime_result=analyzed,
                    strategy_results=[
                        mean_reversion,
                        breakout,
                        trend_pullback,
                        volatility_expansion,
                    ],
                )
                setup = analyzed.get("setup")
                setup_status = (
                    setup.get("status")
                    if isinstance(setup, dict)
                    else analyzed.get("trade_posture")
                )

                results.append(
                    {
                        "symbol": symbol,
                        "status": setup_status,
                        "mean_reversion": mean_reversion,
                        "breakout": breakout,
                        "trend_pullback": trend_pullback,
                        "volatility_expansion": volatility_expansion,
                        "decision": decision,
                        **analyzed,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "symbol": symbol,
                        "status": "ERROR",
                        "regime": REGIME_DATA_ERROR,
                        "confidence": 0,
                        "trade_posture": "NO_TRADE",
                        "reason": f"{type(exc).__name__}:{str(exc)[:180]}",
                        "metrics": {"ok": False, "reason": "exception"},
                    }
                )

        return {
            "status": "OK",
            "read_only": True,
            "execution_disabled": True,
            "symbols_scanned": len(symbols),
            "results": results,
            "summary": summarize_results(results),
        }


def compact_cycle(cycle: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "cycle": cycle.get("cycle"),
        "duration_s": cycle.get("duration_s"),
        "status": cycle.get("status"),
        "read_only": True,
        "execution_disabled": True,
        "symbols_scanned": cycle.get("symbols_scanned"),
        "summary": cycle.get("summary"),
        "results": [compact_result(result) for result in cycle.get("results", [])],
    }
    if isinstance(cycle.get("cycle_error"), dict):
        compact["cycle_error"] = cycle["cycle_error"]
    return compact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--max-symbols", type=int, default=5)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=60.0)
    parser.add_argument("--allow-production-read-only", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument(
        "--progress-jsonl",
        default="",
        help="Append one compact JSON record per completed cycle for unattended runs.",
    )
    parser.add_argument(
        "--final-output",
        default="",
        help="Write the final JSON output to this path in addition to stdout.",
    )
    parser.add_argument(
        "--checkpoint-output",
        default="",
        help="Write a rolling partial JSON output after every cycle for unattended runs.",
    )
    args = parser.parse_args()

    if args.cycles <= 0:
        raise SystemExit("--cycles must be positive")

    validate_read_only_environment(
        demo=config.BYBIT_DEMO,
        testnet=config.BYBIT_TESTNET,
        allow_production_read_only=args.allow_production_read_only,
    )

    symbols = parse_symbols(args.symbols, list(config.SYMBOLS), args.max_symbols)
    if not symbols:
        raise SystemExit("No symbols selected")

    observer = ReadOnlyMarketRegimeObserver()
    cycles: list[dict[str, Any]] = []
    output_errors: list[dict[str, Any]] = []
    interrupted = False

    try:
        for index in range(1, args.cycles + 1):
            started_at = time.time()
            try:
                cycle = observer.run_cycle(symbols)
            except Exception as exc:
                cycle = build_cycle_error(symbols, exc)
            cycle["cycle"] = index
            cycle["duration_s"] = round(time.time() - started_at, 3)
            cycles.append(cycle)

            has_cycle_errors = any(
                isinstance(existing_cycle.get("cycle_error"), dict)
                for existing_cycle in cycles
            )
            checkpoint_status = (
                RUN_STATUS_RUNNING_WITH_ERRORS if has_cycle_errors else RUN_STATUS_RUNNING
            )

            if args.progress_jsonl:
                safe_append_jsonl_file(
                    args.progress_jsonl,
                    {
                        "type": "cycle",
                        "cycle": index,
                        "cycles_requested": args.cycles,
                        "completed_at": round(time.time(), 3),
                        "summary": cycle.get("summary"),
                        "result": compact_cycle(cycle),
                    },
                    output_errors,
                )

            if args.checkpoint_output:
                checkpoint = build_run_output(
                    status=checkpoint_status,
                    cycles_requested=args.cycles,
                    symbols=symbols,
                    cycles=cycles,
                    summary_only=True,
                    output_errors=output_errors,
                )
                safe_write_json_file(args.checkpoint_output, checkpoint, output_errors)

            if index < args.cycles:
                time.sleep(max(args.sleep, 0.0))
    except KeyboardInterrupt:
        interrupted = True

    has_cycle_errors = any(
        isinstance(existing_cycle.get("cycle_error"), dict)
        for existing_cycle in cycles
    )
    if interrupted:
        final_status = RUN_STATUS_INTERRUPTED
    elif has_cycle_errors:
        final_status = RUN_STATUS_COMPLETED_WITH_ERRORS
    else:
        final_status = RUN_STATUS_OK

    output = build_run_output(
        status=final_status,
        cycles_requested=args.cycles,
        symbols=symbols,
        cycles=cycles,
        summary_only=args.summary_only,
        output_errors=output_errors,
    )

    if args.checkpoint_output:
        safe_write_json_file(args.checkpoint_output, output, output_errors)

    if args.final_output:
        safe_write_json_file(args.final_output, output, output_errors)

    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
