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
from engine.strategies.mean_reversion import STATUS_WATCH_ONLY as MR_STATUS_WATCH_ONLY
from engine.strategies.mean_reversion import MeanReversionStrategy
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

    decision = compact_decision(result.get("decision"))
    if decision:
        compact["decision"] = decision

    return compact


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    regime_counts = Counter(str(result.get("regime", "UNKNOWN")) for result in results)
    posture_counts = Counter(str(result.get("trade_posture", "UNKNOWN")) for result in results)
    status_counts = Counter(str(result.get("status", "UNKNOWN")) for result in results)
    mean_reversion_status_counts = Counter(
        str((result.get("mean_reversion") or {}).get("status", "UNKNOWN"))
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
        "range_edge_watches": range_watches[:10],
        "compression_watches": compression_watches[:10],
        "mean_reversion_watches": mean_reversion_watches[:10],
        "mean_reversion_status_counts": dict(sorted(mean_reversion_status_counts.items())),
        "coordinator_decision_counts": dict(sorted(coordinator_decision_counts.items())),
        "high_confidence": high_confidence,
    }


def summarize_cycles(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    all_results: list[dict[str, Any]] = []
    for cycle in cycles:
        results = cycle.get("results")
        if isinstance(results, list):
            all_results.extend(result for result in results if isinstance(result, dict))

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
    summary["errors_total"] = sum(
        1 for result in all_results if result.get("regime") == REGIME_DATA_ERROR
    )
    return summary


class ReadOnlyMarketRegimeObserver:
    def __init__(self) -> None:
        self.exchange = ExchangeManager()
        self.classifier = MarketRegimeClassifier()
        self.mean_reversion = MeanReversionStrategy()
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
                decision = self.coordinator.decide(
                    symbol=symbol,
                    regime_result=analyzed,
                    strategy_results=[mean_reversion],
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
    return {
        "cycle": cycle.get("cycle"),
        "duration_s": cycle.get("duration_s"),
        "status": cycle.get("status"),
        "read_only": True,
        "execution_disabled": True,
        "symbols_scanned": cycle.get("symbols_scanned"),
        "summary": cycle.get("summary"),
        "results": [compact_result(result) for result in cycle.get("results", [])],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--max-symbols", type=int, default=5)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=60.0)
    parser.add_argument("--allow-production-read-only", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
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

    for index in range(1, args.cycles + 1):
        started_at = time.time()
        cycle = observer.run_cycle(symbols)
        cycle["cycle"] = index
        cycle["duration_s"] = round(time.time() - started_at, 3)
        cycles.append(cycle)

        if index < args.cycles:
            time.sleep(max(args.sleep, 0.0))

    output = {
        "status": "OK",
        "read_only": True,
        "execution_disabled": True,
        "environment": {"demo": config.BYBIT_DEMO, "testnet": config.BYBIT_TESTNET},
        "cycles_requested": args.cycles,
        "cycles_completed": len(cycles),
        "symbols": symbols,
        "summary": summarize_cycles(cycles),
        "cycles": [compact_cycle(cycle) for cycle in cycles] if args.summary_only else cycles,
    }

    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
