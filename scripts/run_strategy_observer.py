#!/usr/bin/env python3
"""Read-only strategy observer for demo/testnet market scans.

This script intentionally never imports TradeExecutor and never calls
place_order. It runs the analysis/scoring path and emits JSON summaries so
strategy behavior can be watched without placing orders.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from core.exchange import ExchangeManager
from core.risk_manager import RiskManager
from engine.filters import MarketFilters
from engine.indicators import ConfirmationModule
from engine.liquidity import LiquidityEngine
from engine.scoring import ScoringSystem
from engine.smc_analyzer import SMCAnalyzer
from engine.trend_engine import TrendEngine


def validate_read_only_environment(
    *,
    demo: bool,
    testnet: bool,
    allow_production_read_only: bool,
) -> None:
    if demo or testnet or allow_production_read_only:
        return
    raise RuntimeError(
        "SAFEGUARD: read-only observer requires BYBIT_DEMO=true or "
        "BYBIT_TESTNET=true unless --allow-production-read-only is set"
    )


def parse_symbols(value: str, default_symbols: list[str], max_symbols: int) -> list[str]:
    raw = value.split(",") if value else default_symbols
    symbols: list[str] = []
    seen: set[str] = set()

    for item in raw:
        symbol = str(item).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)

    if max_symbols > 0:
        symbols = symbols[:max_symbols]

    return symbols


def validate_market_data(data: Any) -> tuple[bool, str]:
    required_tfs = ("5m", "15m", "1h", "4h")
    required_cols = {"open", "high", "low", "close", "volume"}
    min_bars = {
        "5m": 80,
        "15m": int(config.SMC_SETTINGS.get("structure_lookback", 120)),
        "1h": int(config.SMC_SETTINGS.get("pd_lookback", 250)),
        "4h": 80,
    }

    if not isinstance(data, dict):
        return False, "missing_data_packet"

    for tf in required_tfs:
        df = data.get(tf)
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False, f"missing_timeframe:{tf}"
        if len(df) < min_bars[tf]:
            return False, f"not_enough_bars:{tf}:{len(df)}/{min_bars[tf]}"
        if not required_cols.issubset(df.columns):
            return False, f"invalid_columns:{tf}"

    return True, "ok"


def news_score_bonus(news_action: str, trend: str) -> int:
    action = str(news_action).upper()
    trend = str(trend).upper()
    if action == "BLOCK":
        return -100
    if action == trend and action in ("LONG", "SHORT"):
        return 15
    if action not in ("NONE", "") and action != trend:
        return -20
    return 0


def effective_liquidity_target(
    *,
    trend: str,
    has_liquidity_target: bool,
    has_eqh: bool,
    has_eql: bool,
    has_ql: bool,
) -> bool:
    if has_liquidity_target:
        return True

    direction = str(trend).upper()
    if direction == "LONG":
        return has_eqh
    if direction == "SHORT":
        return has_eql or has_ql
    return False


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def compact_poi(poi: dict[str, Any] | None) -> dict[str, Any] | None:
    if not poi:
        return None
    return {
        "type": poi.get("type"),
        "side": poi.get("side"),
        "top": safe_float(poi.get("top")),
        "bottom": safe_float(poi.get("bottom")),
        "mid": safe_float(poi.get("mid", poi.get("price"))),
    }


def risk_reward_ratio(entry: float, stop: float, tp: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return abs(tp - entry) / risk


def protective_stop_loss(entry: float, stop: float, trend: str) -> tuple[float, bool]:
    stop_dist_pct = (abs(entry - stop) / entry) * 100.0 if entry > 0 else 0.0
    if stop_dist_pct >= RiskManager.MIN_STOP_PCT:
        return stop, False

    stop_dist = entry * (RiskManager.MIN_STOP_PCT / 100.0)
    adjusted = entry - stop_dist if trend == "LONG" else entry + stop_dist
    return adjusted, True


def configured_min_rr() -> float:
    ratios = config.TRADE_EXECUTION.get("tp_ratios", [])
    return float(ratios[0]) if ratios else RiskManager.DEFAULT_MIN_RR


def build_signal_plan(
    *,
    trend: str,
    current_price: float,
    sl_price: float,
    zone_top: float,
    zone_bottom: float,
    score: int,
    route: str | None,
) -> dict[str, Any]:
    zone_size = abs(zone_top - zone_bottom) or current_price * 0.01
    route_tp = current_price + zone_size * 3 if trend == "LONG" else current_price - zone_size * 3
    execution_entry = (zone_top + zone_bottom) / 2.0 if route == "LIMIT" else current_price
    execution_tp = (
        execution_entry + zone_size * 3
        if trend == "LONG"
        else execution_entry - zone_size * 3
    )
    protective_stop, protective_adjusted = protective_stop_loss(
        execution_entry,
        sl_price,
        trend,
    )

    return {
        "score": score,
        "route": route,
        "min_rr": configured_min_rr(),
        "min_stop_pct": RiskManager.MIN_STOP_PCT,
        "route_reference_entry": current_price,
        "route_reference_tp": route_tp,
        "route_reference_rr": round(risk_reward_ratio(current_price, sl_price, route_tp), 4),
        "execution_entry": execution_entry,
        "execution_tp": execution_tp,
        "execution_rr": round(risk_reward_ratio(execution_entry, sl_price, execution_tp), 4),
        "stop_loss": sl_price,
        "protective_stop_loss": protective_stop,
        "protective_stop_adjusted": protective_adjusted,
        "protective_execution_rr": round(
            risk_reward_ratio(execution_entry, protective_stop, execution_tp),
            4,
        ),
        "zone_top": zone_top,
        "zone_bottom": zone_bottom,
        "zone_size": zone_size,
        "order_type": "Limit" if route == "LIMIT" else "Market",
        "read_only": True,
    }


def failed_checks(result: dict[str, Any]) -> list[str]:
    analysis = result.get("analysis")
    if not isinstance(analysis, dict):
        return []

    blocker_checks = (
        ("structure", "structure_ok"),
        ("poi", "poi_ok"),
        ("m5", "m5_ok"),
        ("macro", "macro_ok"),
        ("pd_alignment", "is_pd_aligned"),
        ("liquidity_target", "has_liquidity_target"),
    )
    return [
        label
        for label, key in blocker_checks
        if key in analysis and not bool(analysis.get(key))
    ]


def poi_blocker_reason(result: dict[str, Any], analysis: dict[str, Any]) -> str:
    poi = result.get("poi")
    if not poi:
        return "missing"

    poi_side = str(poi.get("side", "")).upper()
    trend = str(result.get("trend") or analysis.get("trend", "")).upper()
    if poi_side and trend and poi_side != trend:
        return "wrong_side"

    if "smc_ok" in analysis and not bool(analysis.get("smc_ok")):
        return "smc_not_ok"

    return "not_confirmed"


def blocker_details(result: dict[str, Any]) -> dict[str, Any]:
    analysis = result.get("analysis")
    if not isinstance(analysis, dict):
        return {}

    failed = set(failed_checks(result))
    details: dict[str, Any] = {}

    if "poi" in failed:
        poi = result.get("poi") if isinstance(result.get("poi"), dict) else {}
        details["poi"] = {
            "reason": poi_blocker_reason(result, analysis),
            "side": poi.get("side"),
            "type": poi.get("type"),
            "trend": result.get("trend") or analysis.get("trend"),
            "side_aligned": bool(analysis.get("poi_side_aligned", False)),
            "smc_ok": bool(analysis.get("smc_ok", False)),
        }

    if "m5" in failed:
        metrics = result.get("confirmation_metrics")
        metrics = metrics if isinstance(metrics, dict) else {}
        details["m5"] = {
            "is_trigger": bool(metrics.get("is_trigger", False)),
            "body_ratio": metrics.get("body_ratio"),
            "vol_ratio": metrics.get("vol_ratio"),
            "rsi_velocity": metrics.get("rsi_velocity"),
            "adx_strength": metrics.get("adx_strength"),
        }

    if "pd_alignment" in failed:
        details["pd_alignment"] = {"aligned": bool(analysis.get("is_pd_aligned", False))}

    if "liquidity_target" in failed:
        details["liquidity_target"] = {
            "has_target": bool(analysis.get("has_liquidity_target", False)),
            "has_eqh": bool(analysis.get("has_eqh", False)),
            "has_eql": bool(analysis.get("has_eql", False)),
            "has_ql": bool(analysis.get("has_ql", False)),
            "context": analysis.get("liquidity_context"),
        }

    return details


def execution_wait_checks(analysis: dict[str, Any]) -> list[str]:
    checks = (("m5", "m5_ok"),)
    return [
        label
        for label, key in checks
        if key in analysis and not bool(analysis.get(key))
    ]


def classify_signal_status(
    *,
    score: int,
    threshold: int,
    analysis: dict[str, Any],
) -> tuple[str, str]:
    if score < threshold:
        return "REJECT", f"score_below_threshold:{score}/{threshold}"

    waiting_for = execution_wait_checks(analysis)
    if waiting_for:
        return "WAIT_CONFIRMATION", f"waiting_for:{','.join(waiting_for)}"

    return "SIGNAL", ""


def compact_setup(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "symbol": result.get("symbol", ""),
        "status": result.get("status", ""),
        "trend": result.get("trend", ""),
        "reason": result.get("reason", ""),
        "score": result.get("score"),
        "threshold": result.get("threshold"),
        "would_route": result.get("would_route"),
        "has_poi": bool(result.get("poi")),
    }
    failed = failed_checks(result)
    if failed:
        compact["failed_checks"] = failed
    details = blocker_details(result)
    if details:
        compact["blocker_details"] = details
    plan = result.get("signal_plan")
    if isinstance(plan, dict):
        compact["signal_plan"] = {
            "route": plan.get("route"),
            "order_type": plan.get("order_type"),
            "execution_entry": plan.get("execution_entry"),
            "stop_loss": plan.get("stop_loss"),
            "execution_tp": plan.get("execution_tp"),
            "execution_rr": plan.get("execution_rr"),
            "protective_stop_loss": plan.get("protective_stop_loss"),
            "protective_stop_adjusted": plan.get("protective_stop_adjusted"),
            "protective_execution_rr": plan.get("protective_execution_rr"),
            "route_reference_rr": plan.get("route_reference_rr"),
            "min_rr": plan.get("min_rr"),
            "min_stop_pct": plan.get("min_stop_pct"),
        }
    return compact


def count_blocker_details(setups: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts = {
        "poi_reason_counts": Counter(),
        "poi_type_counts": Counter(),
        "m5_trigger_counts": Counter(),
        "pd_alignment_counts": Counter(),
        "liquidity_target_counts": Counter(),
        "liquidity_context_counts": Counter(),
    }

    for setup in setups:
        details = setup.get("blocker_details")
        if not isinstance(details, dict):
            continue

        poi = details.get("poi")
        if isinstance(poi, dict):
            counts["poi_reason_counts"].update([str(poi.get("reason", "unknown"))])
            counts["poi_type_counts"].update([str(poi.get("type") or "missing")])

        m5 = details.get("m5")
        if isinstance(m5, dict):
            counts["m5_trigger_counts"].update(
                [str(bool(m5.get("is_trigger", False))).lower()]
            )

        pd_alignment = details.get("pd_alignment")
        if isinstance(pd_alignment, dict):
            counts["pd_alignment_counts"].update(
                [str(bool(pd_alignment.get("aligned", False))).lower()]
            )

        liquidity = details.get("liquidity_target")
        if isinstance(liquidity, dict):
            target_key = "present" if liquidity.get("has_target") else "missing"
            counts["liquidity_target_counts"].update([target_key])
            counts["liquidity_context_counts"].update(
                [str(liquidity.get("context") or "unknown")]
            )

    return {
        name: dict(sorted(counter.items()))
        for name, counter in counts.items()
        if counter
    }


def summarize_cycle(cycle: dict[str, Any]) -> dict[str, Any]:
    raw_results = cycle.get("results", [])
    results = raw_results if isinstance(raw_results, list) else []
    status_counts = Counter(
        str(item.get("status", "UNKNOWN")) for item in results if isinstance(item, dict)
    )
    trend_counts = Counter(
        str(item.get("trend", "UNKNOWN")) for item in results if isinstance(item, dict)
    )
    reject_reasons = Counter(
        str(item.get("reason", "unspecified"))
        for item in results
        if isinstance(item, dict) and item.get("status") == "REJECT"
    )
    signals = [
        compact_setup(item)
        for item in results
        if isinstance(item, dict) and item.get("status") == "SIGNAL"
    ]
    waiting_setups = [
        compact_setup(item)
        for item in results
        if isinstance(item, dict) and item.get("status") == "WAIT_CONFIRMATION"
    ]
    errors = [
        compact_setup(item)
        for item in results
        if isinstance(item, dict) and item.get("status") == "ERROR"
    ]
    near_setups = [
        compact_setup(item)
        for item in results
        if isinstance(item, dict)
        and item.get("status") == "REJECT"
        and str(item.get("trend", "")).upper() in ("LONG", "SHORT")
    ]

    return {
        "status_counts": dict(sorted(status_counts.items())),
        "trend_counts": dict(sorted(trend_counts.items())),
        "reject_reasons": dict(sorted(reject_reasons.items())),
        "signals": signals,
        "waiting_setups": waiting_setups,
        "errors": errors,
        "near_setups": near_setups,
        "blocker_detail_counts": count_blocker_details(
            signals + waiting_setups + near_setups
        ),
    }


def summarize_cycles(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    trend_counts: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()
    signals: list[dict[str, Any]] = []
    waiting_setups: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    signals_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    waiting_setups_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    near_setups_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    signal_counts: Counter[str] = Counter()
    signal_route_counts: Counter[str] = Counter()
    signal_failed_check_counts: Counter[str] = Counter()
    waiting_setup_counts: Counter[str] = Counter()
    waiting_setup_route_counts: Counter[str] = Counter()
    waiting_setup_failed_check_counts: Counter[str] = Counter()
    near_setup_counts: Counter[str] = Counter()
    near_setup_failed_check_counts: Counter[str] = Counter()
    blocker_detail_counts: dict[str, Counter[str]] = {}

    for cycle in cycles:
        summary = summarize_cycle(cycle)
        status_counts.update(summary["status_counts"])
        trend_counts.update(summary["trend_counts"])
        reject_reasons.update(summary["reject_reasons"])
        signals.extend(summary["signals"])
        waiting_setups.extend(summary["waiting_setups"])
        errors.extend(summary["errors"])
        for name, values in summary.get("blocker_detail_counts", {}).items():
            counter = blocker_detail_counts.setdefault(name, Counter())
            counter.update(values)
        for signal in summary["signals"]:
            signal_counts.update([str(signal.get("symbol", "UNKNOWN"))])
            signal_route_counts.update([str(signal.get("would_route", "UNKNOWN"))])
            signal_failed_check_counts.update(str(item) for item in signal.get("failed_checks", []))
            key = (
                signal.get("symbol"),
                signal.get("trend"),
                signal.get("would_route"),
                tuple(signal.get("failed_checks", [])),
            )
            signals_by_key[key] = signal
        for setup in summary["waiting_setups"]:
            waiting_setup_counts.update([str(setup.get("symbol", "UNKNOWN"))])
            waiting_setup_route_counts.update(
                [str(setup.get("would_route", "UNKNOWN"))]
            )
            waiting_setup_failed_check_counts.update(
                str(item) for item in setup.get("failed_checks", [])
            )
            key = (
                setup.get("symbol"),
                setup.get("trend"),
                setup.get("would_route"),
                tuple(setup.get("failed_checks", [])),
            )
            waiting_setups_by_key[key] = setup
        for setup in summary["near_setups"]:
            near_setup_counts.update([str(setup.get("symbol", "UNKNOWN"))])
            near_setup_failed_check_counts.update(str(item) for item in setup.get("failed_checks", []))
            key = (
                setup.get("symbol"),
                setup.get("trend"),
                setup.get("reason"),
                tuple(setup.get("failed_checks", [])),
            )
            near_setups_by_key[key] = setup

    return {
        "status_counts": dict(sorted(status_counts.items())),
        "trend_counts": dict(sorted(trend_counts.items())),
        "reject_reasons": dict(sorted(reject_reasons.items())),
        "signals_total": len(signals),
        "waiting_setups_total": len(waiting_setups),
        "errors_total": len(errors),
        "signals": list(signals_by_key.values()),
        "waiting_setups": list(waiting_setups_by_key.values()),
        "errors": errors,
        "near_setups": list(near_setups_by_key.values()),
        "signal_counts": dict(sorted(signal_counts.items())),
        "signal_route_counts": dict(sorted(signal_route_counts.items())),
        "signal_failed_check_counts": dict(sorted(signal_failed_check_counts.items())),
        "waiting_setup_counts": dict(sorted(waiting_setup_counts.items())),
        "waiting_setup_route_counts": dict(sorted(waiting_setup_route_counts.items())),
        "waiting_setup_failed_check_counts": dict(
            sorted(waiting_setup_failed_check_counts.items())
        ),
        "near_setup_counts": dict(sorted(near_setup_counts.items())),
        "near_setup_failed_check_counts": dict(sorted(near_setup_failed_check_counts.items())),
        "blocker_detail_counts": {
            name: dict(sorted(counter.items()))
            for name, counter in sorted(blocker_detail_counts.items())
        },
    }


def compact_cycle(cycle: dict[str, Any]) -> dict[str, Any]:
    return {
        "cycle": cycle.get("cycle"),
        "status": cycle.get("status"),
        "duration_s": cycle.get("duration_s"),
        "symbols_scanned": cycle.get("symbols_scanned"),
        "signals": cycle.get("signals"),
        "waiting_setups": cycle.get("waiting_setups"),
        "macro": cycle.get("macro"),
        "news": cycle.get("news"),
        "summary": cycle.get("summary", summarize_cycle(cycle)),
    }


class ReadOnlyStrategyObserver:
    def __init__(self, *, include_news: bool = False):
        self.exchange = ExchangeManager()
        self.filters = MarketFilters()
        self.smc = SMCAnalyzer()
        self.liquidity = LiquidityEngine()
        self.scoring = ScoringSystem()
        self.confirmation = ConfirmationModule()
        self.risk = RiskManager(balance=1.0)
        self.include_news = include_news

    @staticmethod
    def _news_context(include_news: bool) -> dict[str, Any]:
        if not include_news:
            return {"action": "NONE", "title": "", "score": 0.0, "published": ""}

        from engine.news_filter import NewsFilter

        return dict(NewsFilter().analyze_news())

    def scan_symbol(
        self,
        symbol: str,
        *,
        threshold: int,
        news: dict[str, Any],
        macro: dict[str, float],
    ) -> dict[str, Any]:
        data = self.exchange.fetch_all_timeframes(symbol)
        data_ok, data_reason = validate_market_data(data)
        if not data_ok:
            return {
                "symbol": symbol,
                "status": "REJECT",
                "reason": data_reason,
                "execution_disabled": True,
            }

        assert isinstance(data, dict)
        trend = TrendEngine.get_direction(data["1h"], data["15m"])
        market_ok = self.filters.is_market_suitable(data["1h"])
        filter_metrics = self.filters.get_metrics_snapshot()

        if not market_ok:
            return {
                "symbol": symbol,
                "status": "REJECT",
                "reason": "market_filter",
                "trend": trend,
                "metrics": filter_metrics,
                "execution_disabled": True,
            }

        if trend == "FLAT":
            return {
                "symbol": symbol,
                "status": "FLAT",
                "trend": trend,
                "metrics": filter_metrics,
                "execution_disabled": True,
            }

        mtf_context = self.smc.analyze_mtf(df_htf=data["1h"], df_ltf=data["15m"])
        final_structure = (
            mtf_context.get("ltf_structure")
            if mtf_context.get("ltf_structure", {}).get("is_confirmed")
            else mtf_context.get("htf_structure", {})
        )
        final_poi = mtf_context.get("poi")
        liquidity_15m = self.liquidity.analyze(data["15m"])
        sweep_5m = self.liquidity.check_sweep_pro(data["5m"])
        liquidity_context = self.liquidity.evaluate_liquidity_context(liquidity_15m)
        macro_ok = MarketFilters.check_macro(macro, trend)
        poi_side_aligned = bool(final_poi and final_poi.get("side") == trend)
        smc_ok = bool(mtf_context.get("smc_ok", False))
        raw_liquidity_target = bool(mtf_context.get("has_liquidity_target", False))
        has_eqh = bool(liquidity_15m.get("has_eqh", False))
        has_eql = bool(liquidity_15m.get("has_eql", False))
        has_ql = bool(liquidity_15m.get("has_ql", False))
        has_liquidity_target = effective_liquidity_target(
            trend=trend,
            has_liquidity_target=raw_liquidity_target,
            has_eqh=has_eqh,
            has_eql=has_eql,
            has_ql=has_ql,
        )

        analysis = {
            "trend": trend,
            "direction": trend,
            "trend_ok": True,
            "structure_ok": bool(final_structure.get("is_confirmed", False)),
            "poi_ok": poi_side_aligned and smc_ok,
            "smc_ok": smc_ok,
            "poi_side_aligned": poi_side_aligned,
            "poi_side": final_poi.get("side") if final_poi else None,
            "m5_ok": self.confirmation.check_m5_entry(data["5m"], trend),
            "macro_ok": macro_ok,
            "liquidity_sweep": bool(sweep_5m.get("is_confirmed", False)),
            "sweep_active": bool(sweep_5m.get("is_confirmed", False)),
            "is_pd_aligned": bool(mtf_context.get("is_pd_aligned", False)),
            "has_liquidity_target": has_liquidity_target,
            "raw_has_liquidity_target": raw_liquidity_target,
            "has_eqh": has_eqh,
            "has_eql": has_eql,
            "has_ql": has_ql,
            "high_volatility": False,
            "news_action": news.get("action", "NONE"),
            "liquidity_context": liquidity_context,
        }

        score = max(
            0,
            min(
                100,
                self.scoring.calculate(analysis)
                + news_score_bonus(news.get("action", "NONE"), trend),
            ),
        )
        status, reason = classify_signal_status(
            score=score,
            threshold=threshold,
            analysis=analysis,
        )
        rr_status = None
        signal_plan = None

        if status in ("SIGNAL", "WAIT_CONFIRMATION") and final_poi:
            current_price = safe_float(data["1h"]["close"].iloc[-2])
            sl_raw = final_poi.get("bottom") if trend == "LONG" else final_poi.get("top")
            sl_price = safe_float(sl_raw, current_price)
            if trend == "SHORT" and sl_price <= current_price:
                sl_price = current_price * 1.005
            elif trend == "LONG" and sl_price >= current_price:
                sl_price = current_price * 0.995

            zone_top = safe_float(final_poi.get("top"), current_price)
            zone_bottom = safe_float(final_poi.get("bottom"), current_price)
            zone_size = abs(zone_top - zone_bottom) or current_price * 0.01
            tp_price = current_price + zone_size * 3 if trend == "LONG" else current_price - zone_size * 3
            rr_status = self.risk.validate_risk_reward(current_price, sl_price, tp_price, score=score)
            signal_plan = build_signal_plan(
                trend=trend,
                current_price=current_price,
                sl_price=sl_price,
                zone_top=zone_top,
                zone_bottom=zone_bottom,
                score=score,
                route=rr_status,
            )

            if rr_status == "REJECT":
                status = "REJECT"
                reason = "risk_reward_reject"

        return {
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "trend": trend,
            "score": score,
            "threshold": threshold,
            "would_route": rr_status,
            "execution_disabled": True,
            "analysis": analysis,
            "filter_metrics": filter_metrics,
            "confirmation_metrics": self.confirmation.get_metrics_snapshot(),
            "poi": compact_poi(final_poi),
            "signal_plan": signal_plan,
        }

    def run_cycle(self, symbols: list[str], *, threshold: int) -> dict[str, Any]:
        news = self._news_context(self.include_news)
        macro = self.exchange.fetch_macro_indices()
        results: list[dict[str, Any]] = []

        for symbol in symbols:
            try:
                results.append(self.scan_symbol(symbol, threshold=threshold, news=news, macro=macro))
            except Exception as exc:
                results.append(
                    {
                        "symbol": symbol,
                        "status": "ERROR",
                        "reason": str(exc)[:240],
                        "execution_disabled": True,
                    }
                )

        signals = [item for item in results if item.get("status") == "SIGNAL"]
        waiting_setups = [
            item for item in results if item.get("status") == "WAIT_CONFIRMATION"
        ]
        errors = [item for item in results if item.get("status") == "ERROR"]
        return {
            "status": "ERROR" if errors else "OK",
            "read_only": True,
            "execution_disabled": True,
            "news": news,
            "macro": macro,
            "symbols_scanned": len(results),
            "signals": len(signals),
            "waiting_setups": len(waiting_setups),
            "results": results,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--max-symbols", type=int, default=5)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--sleep", type=float, default=60.0)
    parser.add_argument("--include-news", action="store_true")
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

    threshold = int(config.get_current_risk().get("min_score_to_enter", 55))
    observer = ReadOnlyStrategyObserver(include_news=args.include_news)
    cycles: list[dict[str, Any]] = []

    for index in range(1, args.cycles + 1):
        started_at = time.time()
        cycle = observer.run_cycle(symbols, threshold=threshold)
        cycle["cycle"] = index
        cycle["duration_s"] = round(time.time() - started_at, 3)
        cycle["summary"] = summarize_cycle(cycle)
        cycles.append(cycle)

        if index < args.cycles:
            time.sleep(max(args.sleep, 0.0))

    failed = [cycle for cycle in cycles if cycle.get("status") != "OK"]
    output = {
        "status": "ERROR" if failed else "OK",
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
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
