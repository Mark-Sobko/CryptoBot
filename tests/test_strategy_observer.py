import math
import os
import unittest

import pandas as pd

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from scripts.run_strategy_observer import (
    blocker_details,
    build_signal_plan,
    classify_signal_status,
    compact_cycle,
    compact_setup,
    count_blocker_details,
    count_setup_roles,
    closest_structure_setups,
    distance_bucket_pct,
    effective_liquidity_target,
    execution_waits,
    news_score_bonus,
    hard_blockers,
    missing_confluences,
    parse_symbols,
    protective_stop_loss,
    risk_reward_ratio,
    setup_state,
    smc_blocker_reason,
    structure_diagnostics,
    summarize_cycle,
    summarize_cycles,
    validate_market_data,
    validate_read_only_environment,
)
from engine.smc_analyzer import SMCAnalyzer
from engine.smc.structure_engine import StructureEngine


class StrategyObserverTests(unittest.TestCase):
    def test_read_only_environment_blocks_production_by_default(self):
        with self.assertRaises(RuntimeError):
            validate_read_only_environment(
                demo=False,
                testnet=False,
                allow_production_read_only=False,
            )

        validate_read_only_environment(
            demo=False,
            testnet=False,
            allow_production_read_only=True,
        )
        validate_read_only_environment(
            demo=True,
            testnet=False,
            allow_production_read_only=False,
        )

    def test_parse_symbols_normalizes_deduplicates_and_limits(self):
        symbols = parse_symbols(" btcusdt,ETHUSDT, btcusdt, solusdt ", ["XRPUSDT"], 2)

        self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT"])

    def test_news_score_bonus_matches_main_direction_logic(self):
        self.assertEqual(news_score_bonus("LONG", "LONG"), 15)
        self.assertEqual(news_score_bonus("SHORT", "LONG"), -20)
        self.assertEqual(news_score_bonus("NONE", "LONG"), 0)
        self.assertEqual(news_score_bonus("BLOCK", "LONG"), -100)

    def test_effective_liquidity_target_matches_scoring_fallback(self):
        self.assertTrue(
            effective_liquidity_target(
                trend="LONG",
                has_liquidity_target=False,
                has_eqh=True,
                has_eql=False,
                has_ql=False,
            )
        )
        self.assertTrue(
            effective_liquidity_target(
                trend="SHORT",
                has_liquidity_target=False,
                has_eqh=False,
                has_eql=False,
                has_ql=True,
            )
        )
        self.assertFalse(
            effective_liquidity_target(
                trend="LONG",
                has_liquidity_target=False,
                has_eqh=False,
                has_eql=True,
                has_ql=True,
            )
        )

    def test_validate_market_data_accepts_required_timeframes(self):
        frame = pd.DataFrame(
            {
                "open": [1.0] * 100,
                "high": [1.1] * 100,
                "low": [0.9] * 100,
                "close": [1.0] * 100,
                "volume": [10.0] * 100,
            }
        )

        ok, reason = validate_market_data(
            {
                "5m": frame,
                "15m": frame,
                "1h": frame,
                "4h": frame,
            }
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_validate_market_data_reports_missing_timeframe(self):
        ok, reason = validate_market_data({})

        self.assertFalse(ok)
        self.assertEqual(reason, "missing_timeframe:5m")

    def test_build_signal_plan_reports_limit_entry_and_rr(self):
        plan = build_signal_plan(
            trend="SHORT",
            current_price=0.153,
            sl_price=0.1542,
            zone_top=0.1542,
            zone_bottom=0.1539,
            score=95,
            route="LIMIT",
        )

        self.assertEqual(plan["order_type"], "Limit")
        self.assertAlmostEqual(plan["execution_entry"], 0.15405)
        self.assertAlmostEqual(plan["execution_tp"], 0.15315)
        self.assertLess(plan["route_reference_rr"], plan["min_rr"])
        self.assertGreater(plan["execution_rr"], plan["min_rr"])
        self.assertTrue(plan["protective_stop_adjusted"])
        self.assertAlmostEqual(plan["protective_stop_loss"], 0.154589175)
        self.assertGreater(plan["protective_execution_rr"], plan["min_rr"])
        self.assertTrue(plan["read_only"])

    def test_risk_reward_ratio_handles_zero_risk(self):
        self.assertEqual(risk_reward_ratio(1.0, 1.0, 1.2), 0.0)
        self.assertAlmostEqual(risk_reward_ratio(1.0, 0.9, 1.2), 2.0)

    def test_protective_stop_loss_preserves_wide_stop(self):
        stop, adjusted = protective_stop_loss(1.0, 0.99, "LONG")

        self.assertEqual(stop, 0.99)
        self.assertFalse(adjusted)

    def test_protective_stop_loss_adjusts_tight_short_stop(self):
        stop, adjusted = protective_stop_loss(1.0, 1.001, "SHORT")

        self.assertAlmostEqual(stop, 1.0035)
        self.assertTrue(adjusted)

    def test_classify_signal_status_waits_for_m5_confirmation(self):
        status, reason = classify_signal_status(
            score=95,
            threshold=55,
            analysis={"m5_ok": False},
        )

        self.assertEqual(status, "WAIT_CONFIRMATION")
        self.assertEqual(reason, "waiting_for:m5")

        status, reason = classify_signal_status(
            score=95,
            threshold=55,
            analysis={"m5_ok": True},
        )

        self.assertEqual(status, "SIGNAL")
        self.assertEqual(reason, "")

    def test_setup_role_helpers_separate_hard_wait_and_confluence_checks(self):
        rejected = {
            "status": "REJECT",
            "trend": "SHORT",
            "analysis": {
                "direction": "SHORT",
                "structure_ok": True,
                "poi_ok": False,
                "m5_ok": False,
                "macro_ok": True,
                "is_pd_aligned": False,
                "has_liquidity_target": False,
                "news_action": "NONE",
            },
        }
        waiting = {
            "status": "WAIT_CONFIRMATION",
            "trend": "SHORT",
            "analysis": {
                "direction": "SHORT",
                "structure_ok": True,
                "poi_ok": True,
                "m5_ok": False,
                "macro_ok": True,
                "is_pd_aligned": True,
                "has_liquidity_target": True,
                "news_action": "NONE",
            },
        }

        self.assertEqual(hard_blockers(rejected), ["poi"])
        self.assertEqual(
            missing_confluences(rejected),
            ["m5", "pd_alignment", "liquidity_target"],
        )
        self.assertEqual(execution_waits(rejected), [])
        self.assertEqual(hard_blockers(waiting), [])
        self.assertEqual(missing_confluences(waiting), [])
        self.assertEqual(execution_waits(waiting), ["m5"])

    def test_hard_blockers_accept_sweep_as_structure_confirmation(self):
        result = {
            "status": "REJECT",
            "trend": "LONG",
            "analysis": {
                "direction": "LONG",
                "structure_ok": False,
                "liquidity_sweep": True,
                "poi_ok": True,
            },
        }

        self.assertEqual(hard_blockers(result), [])

    def test_setup_state_prioritizes_structure_waits(self):
        self.assertEqual(
            setup_state(
                {
                    "status": "REJECT",
                    "trend": "LONG",
                    "analysis": {
                        "direction": "LONG",
                        "htf_direction": None,
                        "htf_structure_ok": False,
                        "ltf_direction": "LONG",
                        "ltf_structure_ok": True,
                        "poi_ok": False,
                        "m5_ok": False,
                        "is_pd_aligned": False,
                    },
                }
            ),
            "WAIT_HTF_STRUCTURE",
        )
        self.assertEqual(
            setup_state(
                {
                    "status": "REJECT",
                    "trend": "SHORT",
                    "analysis": {
                        "direction": "SHORT",
                        "htf_direction": "SHORT",
                        "htf_structure_ok": True,
                        "ltf_direction": None,
                        "ltf_structure_ok": False,
                        "poi_ok": False,
                    },
                }
            ),
            "WAIT_LTF_STRUCTURE",
        )
        self.assertEqual(
            setup_state(
                {
                    "status": "REJECT",
                    "trend": "SHORT",
                    "analysis": {
                        "direction": "SHORT",
                        "htf_direction": "SHORT",
                        "htf_structure_ok": True,
                        "ltf_direction": "SHORT",
                        "ltf_structure_ok": True,
                        "poi_ok": False,
                        "poi_side_aligned": False,
                    },
                }
            ),
            "WAIT_POI_SIDE",
        )

    def test_smc_blocker_reason_reports_mtf_cause(self):
        self.assertEqual(
            smc_blocker_reason({"smc_ok": False, "mtf_aligned": False}),
            "mtf_not_aligned",
        )
        self.assertEqual(
            smc_blocker_reason(
                {
                    "smc_ok": False,
                    "mtf_aligned": False,
                    "htf_direction": None,
                    "ltf_direction": "SHORT",
                    "htf_structure_ok": False,
                }
            ),
            "htf_direction_missing",
        )
        self.assertEqual(
            smc_blocker_reason(
                {
                    "smc_ok": False,
                    "mtf_aligned": False,
                    "htf_direction": "SHORT",
                    "ltf_direction": None,
                    "htf_structure_ok": True,
                }
            ),
            "ltf_direction_missing",
        )
        self.assertEqual(
            smc_blocker_reason(
                {
                    "smc_ok": False,
                    "mtf_aligned": True,
                    "htf_direction": "LONG",
                    "ltf_direction": "LONG",
                    "htf_structure_ok": False,
                }
            ),
            "htf_structure_not_ok",
        )
        self.assertEqual(
            smc_blocker_reason(
                {
                    "smc_ok": False,
                    "mtf_aligned": False,
                    "mtf_direction_aligned": True,
                    "htf_direction": "LONG",
                    "ltf_direction": "LONG",
                    "htf_structure_ok": True,
                    "ltf_structure_ok": False,
                }
            ),
            "ltf_structure_not_ok",
        )
        self.assertEqual(
            smc_blocker_reason(
                {
                    "smc_ok": False,
                    "mtf_aligned": False,
                    "mtf_direction_aligned": False,
                    "htf_direction": "LONG",
                    "ltf_direction": "SHORT",
                    "htf_structure_ok": True,
                    "ltf_structure_ok": True,
                    "poi_side_aligned": True,
                }
            ),
            "mtf_direction_mismatch",
        )

    def test_smc_analyzer_requires_valid_ltf_structure_for_mtf_alignment(self):
        analyzer = SMCAnalyzer()
        htf_frame = object()
        ltf_frame = object()
        htf_structure = {
            "direction": "LONG",
            "structure_ok": True,
            "is_confirmed": True,
        }
        ltf_structure = {
            "direction": "LONG",
            "structure_ok": False,
            "is_confirmed": True,
        }

        def detect_structure(frame):
            return htf_structure if frame is htf_frame else ltf_structure

        analyzer.detect_structure = detect_structure
        analyzer.find_poi = lambda _df: {"side": "LONG", "mid": 0.9}
        analyzer.get_pd_zones = lambda _df: {"equilibrium": 1.0}
        analyzer.detect_liquidity_pools = lambda _df: {"has_eqh": True, "has_eql": False}

        result = analyzer.analyze_mtf(htf_frame, ltf_frame)

        self.assertTrue(result["mtf_direction_aligned"])
        self.assertFalse(result["mtf_aligned"])
        self.assertFalse(result["smc_ok"])

        ltf_structure["structure_ok"] = True
        result = analyzer.analyze_mtf(htf_frame, ltf_frame)

        self.assertTrue(result["mtf_direction_aligned"])
        self.assertTrue(result["mtf_aligned"])
        self.assertTrue(result["smc_ok"])

    def test_structure_engine_reports_non_signal_reasons(self):
        engine = StructureEngine()
        short_frame = pd.DataFrame(
            {
                "open": [1.0] * 10,
                "high": [1.1] * 10,
                "low": [0.9] * 10,
                "close": [1.0] * 10,
                "volume": [10.0] * 10,
            }
        )
        flat_frame = pd.DataFrame(
            {
                "open": [1.0] * 100,
                "high": [1.0] * 100,
                "low": [1.0] * 100,
                "close": [1.0] * 100,
                "volume": [10.0] * 100,
            }
        )

        self.assertEqual(engine.detect_structure(short_frame)["reason"], "invalid_ohlcv")
        flat_result = engine.detect_structure(flat_frame)
        self.assertEqual(flat_result["reason"], "no_swings")
        self.assertEqual(flat_result["swing_highs_count"], 0)
        self.assertEqual(flat_result["swing_lows_count"], 0)

    def test_structure_engine_reports_major_level_proximity(self):
        engine = StructureEngine()
        rows = []
        for i in range(100):
            base = 100.0 + math.sin(i / 3.0) * 2.0 if i < 90 else 100.0
            rows.append(
                {
                    "open": base - 0.1,
                    "high": base + 0.8,
                    "low": base - 0.8,
                    "close": base,
                    "volume": 100.0,
                }
            )
        frame = pd.DataFrame(rows)

        result = engine.detect_structure(frame)

        self.assertEqual(result["reason"], "no_break_or_sweep")
        self.assertIsNotNone(result["nearest_major_high"])
        self.assertIsNotNone(result["nearest_major_low"])
        self.assertIsNotNone(result["closest_level_side"])
        self.assertIsNotNone(result["closest_level_distance_pct"])
        self.assertGreaterEqual(result["distance_to_major_high_pct"], 0)
        self.assertGreaterEqual(result["distance_to_major_low_pct"], 0)

    def test_structure_diagnostics_compacts_prefixed_analysis(self):
        compact = structure_diagnostics(
            "htf",
            {
                "htf_structure_type": "BOS",
                "htf_direction": "LONG",
                "htf_structure_confirmed": True,
                "htf_structure_ok": False,
                "htf_structure_reason": "displacement_not_valid",
                "htf_market_phase": "BULLISH",
                "htf_displacement": {
                    "valid": False,
                    "strength": 0.72,
                    "volume_confirmed": True,
                },
                "htf_swing_highs_count": 5,
                "htf_swing_lows_count": 4,
                "htf_scan_depth": 12,
                "htf_nearest_major_high": 1.05,
                "htf_nearest_major_low": 0.95,
                "htf_distance_to_major_high_pct": 0.42,
                "htf_distance_to_major_low_pct": 2.15,
                "htf_closest_level_side": "HIGH",
                "htf_closest_level_distance_pct": 0.42,
            },
        )

        self.assertEqual(compact["type"], "BOS")
        self.assertEqual(compact["reason"], "displacement_not_valid")
        self.assertFalse(compact["displacement_valid"])
        self.assertEqual(compact["displacement_strength"], 0.72)
        self.assertEqual(compact["closest_level_side"], "HIGH")
        self.assertEqual(compact["closest_level_distance_pct"], 0.42)
        self.assertEqual(distance_bucket_pct(compact["closest_level_distance_pct"]), "<=0.50%")
        self.assertTrue(compact["volume_confirmed"])

    def test_blocker_details_reports_missing_poi_and_m5_metrics(self):
        details = blocker_details(
            {
                "status": "REJECT",
                "trend": "LONG",
                "analysis": {
                    "poi_ok": False,
                    "m5_ok": False,
                    "is_pd_aligned": False,
                    "has_liquidity_target": False,
                    "mtf_aligned": False,
                    "htf_direction": "LONG",
                    "ltf_direction": "SHORT",
                    "has_eql": True,
                    "liquidity_context": "IMBALANCE_DRIVEN",
                },
                "confirmation_metrics": {
                    "is_trigger": False,
                    "body_ratio": 0.35,
                    "vol_ratio": 2.55,
                    "rsi_velocity": 5.07,
                    "adx_strength": 36.18,
                },
            }
        )

        self.assertEqual(details["poi"]["reason"], "missing")
        self.assertEqual(details["poi"]["smc_reason"], "mtf_not_aligned")
        self.assertFalse(details["m5"]["is_trigger"])
        self.assertEqual(details["liquidity_target"]["context"], "IMBALANCE_DRIVEN")

    def test_blocker_details_reports_wrong_side_poi(self):
        details = blocker_details(
            {
                "status": "REJECT",
                "trend": "SHORT",
                "poi": {"side": "LONG", "type": "OB"},
                "analysis": {
                    "poi_ok": False,
                    "poi_side_aligned": False,
                    "smc_ok": True,
                    "m5_ok": True,
                    "is_pd_aligned": True,
                    "has_liquidity_target": True,
                },
            }
        )

        self.assertEqual(details["poi"]["reason"], "wrong_side")
        self.assertEqual(details["poi"]["side"], "LONG")
        self.assertEqual(details["poi"]["trend"], "SHORT")

    def test_count_blocker_details_reports_aggregate_reasons(self):
        counts = count_blocker_details(
            [
                {
                    "blocker_details": {
                        "poi": {"reason": "missing", "type": None},
                        "m5": {"is_trigger": False},
                        "pd_alignment": {"aligned": False},
                        "liquidity_target": {
                            "has_target": False,
                            "context": "IMBALANCE_DRIVEN",
                        },
                    }
                },
                {
                    "blocker_details": {
                        "poi": {
                            "reason": "smc_not_ok",
                            "type": "FVG",
                            "smc_reason": "htf_direction_missing",
                            "mtf_aligned": False,
                            "htf_direction": None,
                            "ltf_direction": "SHORT",
                            "htf_structure_ok": False,
                            "ltf_structure_ok": True,
                            "htf_structure": {
                                "reason": "no_break_or_sweep",
                                "type": None,
                                "market_phase": "FLAT",
                                "displacement_valid": False,
                                "closest_level_side": "HIGH",
                                "closest_level_distance_pct": 0.42,
                            },
                            "ltf_structure": {
                                "reason": "sweep_confirmed",
                                "type": "SWEEP",
                                "market_phase": "BEARISH",
                                "displacement_valid": False,
                                "closest_level_side": "LOW",
                                "closest_level_distance_pct": 2.5,
                            },
                            "side_aligned": True,
                        },
                        "pd_alignment": {"aligned": False},
                        "liquidity_target": {
                            "has_target": False,
                            "context": "IMBALANCE_DRIVEN",
                        },
                    }
                },
            ]
        )

        self.assertEqual(counts["poi_reason_counts"], {"missing": 1, "smc_not_ok": 1})
        self.assertEqual(
            counts["poi_smc_reason_counts"],
            {"htf_direction_missing": 1, "unknown": 1},
        )
        self.assertEqual(counts["poi_type_counts"], {"FVG": 1, "missing": 1})
        self.assertEqual(counts["mtf_alignment_counts"], {"false": 1})
        self.assertEqual(counts["htf_direction_counts"], {"missing": 1})
        self.assertEqual(counts["ltf_direction_counts"], {"SHORT": 1})
        self.assertEqual(counts["htf_structure_ok_counts"], {"false": 1})
        self.assertEqual(counts["ltf_structure_ok_counts"], {"true": 1})
        self.assertEqual(counts["htf_structure_reason_counts"], {"no_break_or_sweep": 1})
        self.assertEqual(counts["ltf_structure_reason_counts"], {"sweep_confirmed": 1})
        self.assertEqual(counts["htf_structure_type_counts"], {"missing": 1})
        self.assertEqual(counts["ltf_structure_type_counts"], {"SWEEP": 1})
        self.assertEqual(counts["htf_market_phase_counts"], {"FLAT": 1})
        self.assertEqual(counts["ltf_market_phase_counts"], {"BEARISH": 1})
        self.assertEqual(counts["htf_displacement_valid_counts"], {"false": 1})
        self.assertEqual(counts["ltf_displacement_valid_counts"], {"false": 1})
        self.assertEqual(counts["htf_closest_level_side_counts"], {"HIGH": 1})
        self.assertEqual(counts["ltf_closest_level_side_counts"], {"LOW": 1})
        self.assertEqual(
            counts["htf_closest_level_distance_bucket_counts"],
            {"<=0.50%": 1},
        )
        self.assertEqual(
            counts["ltf_closest_level_distance_bucket_counts"],
            {">2.00%": 1},
        )
        self.assertEqual(counts["poi_side_alignment_counts"], {"true": 1})
        self.assertEqual(counts["m5_trigger_counts"], {"false": 1})
        self.assertEqual(counts["pd_alignment_counts"], {"false": 2})
        self.assertEqual(counts["liquidity_target_counts"], {"missing": 2})

    def test_count_setup_roles_reports_separate_aggregate_categories(self):
        counts = count_setup_roles(
            [
                {
                    "hard_blockers": ["poi"],
                    "missing_confluences": ["m5", "pd_alignment"],
                },
                {
                    "execution_waits": ["m5"],
                    "missing_confluences": ["liquidity_target"],
                },
            ]
        )

        self.assertEqual(counts["hard_blocker_counts"], {"poi": 1})
        self.assertEqual(
            counts["missing_confluence_counts"],
            {"liquidity_target": 1, "m5": 1, "pd_alignment": 1},
        )
        self.assertEqual(counts["execution_wait_counts"], {"m5": 1})

    def test_summarize_cycle_reports_counts_reasons_and_near_setups(self):
        cycle = {
            "results": [
                {"symbol": "BTCUSDT", "status": "FLAT", "trend": "FLAT"},
                {
                    "symbol": "ETHUSDT",
                    "status": "REJECT",
                    "trend": "LONG",
                    "reason": "score_below_threshold:0/55",
                    "score": 0,
                    "threshold": 55,
                    "analysis": {
                        "structure_ok": True,
                        "poi_ok": False,
                        "m5_ok": False,
                        "macro_ok": True,
                        "is_pd_aligned": False,
                        "has_liquidity_target": False,
                        "liquidity_context": "IMBALANCE_DRIVEN",
                    },
                    "confirmation_metrics": {"is_trigger": False},
                },
                {
                    "symbol": "LTCUSDT",
                    "status": "REJECT",
                    "trend": "FLAT",
                    "reason": "market_filter",
                },
            ],
        }

        summary = summarize_cycle(cycle)

        self.assertEqual(summary["status_counts"], {"FLAT": 1, "REJECT": 2})
        self.assertEqual(
            summary["setup_state_counts"],
            {"FLAT": 1, "MARKET_FILTER": 1, "WAIT_POI": 1},
        )
        self.assertEqual(
            summary["reject_reasons"],
            {"market_filter": 1, "score_below_threshold:0/55": 1},
        )
        self.assertEqual(len(summary["near_setups"]), 1)
        self.assertEqual(summary["near_setups"][0]["symbol"], "ETHUSDT")
        self.assertEqual(summary["near_setups"][0]["setup_state"], "WAIT_POI")
        self.assertEqual(
            summary["near_setups"][0]["failed_checks"],
            ["poi", "m5", "pd_alignment", "liquidity_target"],
        )
        self.assertEqual(summary["near_setups"][0]["hard_blockers"], ["poi"])
        self.assertEqual(
            summary["near_setups"][0]["missing_confluences"],
            ["m5", "pd_alignment", "liquidity_target"],
        )
        self.assertEqual(
            summary["near_setups"][0]["blocker_details"]["poi"]["reason"],
            "missing",
        )
        self.assertEqual(summary["setup_role_counts"]["hard_blocker_counts"], {"poi": 1})
        self.assertEqual(
            summary["blocker_detail_counts"]["poi_reason_counts"],
            {"missing": 1},
        )

    def test_summarize_cycles_reports_signal_and_near_setup_frequencies(self):
        near_setup_cycle = {
            "results": [
                {
                    "symbol": "RENDERUSDT",
                    "status": "REJECT",
                    "trend": "LONG",
                    "reason": "score_below_threshold:0/55",
                    "analysis": {"poi_ok": False, "m5_ok": False},
                }
            ]
        }
        waiting_cycle = {
            "results": [
                {
                    "symbol": "WIFUSDT",
                    "status": "WAIT_CONFIRMATION",
                    "reason": "waiting_for:m5",
                    "trend": "SHORT",
                    "score": 95,
                    "threshold": 55,
                    "would_route": "LIMIT",
                    "poi": {"side": "SHORT"},
                    "analysis": {"poi_ok": True, "m5_ok": False},
                    "signal_plan": {
                        "route": "LIMIT",
                        "order_type": "Limit",
                        "execution_entry": 0.15405,
                        "stop_loss": 0.1542,
                        "execution_tp": 0.15315,
                        "execution_rr": 6.0,
                        "protective_stop_loss": 0.154589175,
                        "protective_stop_adjusted": True,
                        "protective_execution_rr": 1.6692,
                        "route_reference_rr": 0.75,
                        "min_rr": 1.0,
                        "min_stop_pct": 0.35,
                    },
                }
            ]
        }
        signal_cycle = {
            "results": [
                {
                    "symbol": "WIFUSDT",
                    "status": "SIGNAL",
                    "trend": "SHORT",
                    "score": 95,
                    "threshold": 55,
                    "would_route": "LIMIT",
                    "poi": {"side": "SHORT"},
                    "analysis": {"poi_ok": True, "m5_ok": True},
                    "signal_plan": {
                        "route": "LIMIT",
                        "order_type": "Limit",
                        "execution_entry": 0.15405,
                        "stop_loss": 0.1542,
                        "execution_tp": 0.15315,
                        "execution_rr": 6.0,
                        "protective_stop_loss": 0.154589175,
                        "protective_stop_adjusted": True,
                        "protective_execution_rr": 1.6692,
                        "route_reference_rr": 0.75,
                        "min_rr": 1.0,
                        "min_stop_pct": 0.35,
                    },
                }
            ]
        }

        summary = summarize_cycles([
            near_setup_cycle,
            near_setup_cycle,
            waiting_cycle,
            signal_cycle,
        ])

        self.assertEqual(
            summary["status_counts"],
            {"REJECT": 2, "SIGNAL": 1, "WAIT_CONFIRMATION": 1},
        )
        self.assertEqual(
            summary["setup_state_counts"],
            {"SIGNAL_READY": 1, "WAIT_CONFIRMATION": 1, "WAIT_POI": 2},
        )
        self.assertEqual(summary["signals_total"], 1)
        self.assertEqual(summary["waiting_setups_total"], 1)
        self.assertEqual(summary["errors_total"], 0)
        self.assertEqual(len(summary["signals"]), 1)
        self.assertEqual(summary["signals"][0]["signal_plan"]["order_type"], "Limit")
        self.assertEqual(summary["signals"][0]["signal_plan"]["execution_rr"], 6.0)
        self.assertTrue(summary["signals"][0]["signal_plan"]["protective_stop_adjusted"])
        self.assertEqual(len(summary["waiting_setups"]), 1)
        self.assertEqual(summary["waiting_setups"][0]["failed_checks"], ["m5"])
        self.assertEqual(summary["signal_counts"], {"WIFUSDT": 1})
        self.assertEqual(summary["signal_route_counts"], {"LIMIT": 1})
        self.assertEqual(summary["signal_failed_check_counts"], {})
        self.assertEqual(summary["waiting_setup_counts"], {"WIFUSDT": 1})
        self.assertEqual(summary["waiting_setup_route_counts"], {"LIMIT": 1})
        self.assertEqual(summary["waiting_setup_failed_check_counts"], {"m5": 1})
        self.assertEqual(summary["waiting_setup_execution_wait_counts"], {"m5": 1})
        self.assertEqual(len(summary["near_setups"]), 1)
        self.assertEqual(summary["near_setup_counts"], {"RENDERUSDT": 2})
        self.assertEqual(summary["near_setup_failed_check_counts"], {"m5": 2, "poi": 2})
        self.assertEqual(summary["near_setup_hard_blocker_counts"], {"poi": 2})
        self.assertEqual(summary["near_setup_missing_confluence_counts"], {"m5": 2})
        self.assertEqual(
            summary["blocker_detail_counts"]["poi_reason_counts"],
            {"missing": 2},
        )

    def test_summarize_cycles_reports_closest_structure_setups(self):
        first = {
            "symbol": "POLUSDT",
            "status": "REJECT",
            "setup_state": "WAIT_HTF_STRUCTURE",
            "trend": "SHORT",
            "reason": "score_below_threshold:0/55",
            "failed_checks": ["structure", "poi"],
            "blocker_details": {
                "poi": {
                    "htf_structure": {
                        "closest_level_distance_pct": 0.8,
                        "closest_level_side": "LOW",
                        "nearest_major_high": 0.083,
                        "nearest_major_low": 0.079,
                        "reason": "no_break_or_sweep",
                        "structure_ok": False,
                    },
                    "ltf_structure": {
                        "closest_level_distance_pct": 0.3,
                        "closest_level_side": "LOW",
                        "reason": "sweep_confirmed",
                        "structure_ok": True,
                    },
                }
            },
        }
        closer = {
            **first,
            "blocker_details": {
                "poi": {
                    "htf_structure": {
                        "closest_level_distance_pct": 0.42,
                        "closest_level_side": "LOW",
                        "nearest_major_high": 0.083,
                        "nearest_major_low": 0.079,
                        "reason": "no_break_or_sweep",
                        "structure_ok": False,
                    },
                    "ltf_structure": {
                        "closest_level_distance_pct": 0.25,
                        "closest_level_side": "LOW",
                        "reason": "sweep_confirmed",
                        "structure_ok": True,
                    },
                }
            },
        }
        farther = {
            **first,
            "symbol": "LINKUSDT",
            "trend": "LONG",
            "blocker_details": {
                "poi": {
                    "htf_structure": {
                        "closest_level_distance_pct": 1.4,
                        "closest_level_side": "HIGH",
                        "reason": "no_break_or_sweep",
                        "structure_ok": False,
                    }
                }
            },
        }

        summary = summarize_cycles(
            [
                {"results": [first, farther]},
                {"results": [closer]},
            ]
        )

        self.assertEqual(summary["closest_htf_setups"][0]["symbol"], "POLUSDT")
        self.assertEqual(summary["closest_htf_setups"][0]["distance_pct"], 0.42)
        self.assertEqual(summary["closest_htf_setups"][0]["distance_bucket"], "<=0.50%")
        self.assertEqual(summary["closest_ltf_setups"][0]["distance_pct"], 0.25)
        self.assertEqual(
            closest_structure_setups([first, closer], timeframe="htf")[0]["distance_pct"],
            0.42,
        )

    def test_compact_setup_includes_compact_signal_plan(self):
        compact = compact_setup(
            {
                "symbol": "WIFUSDT",
                "status": "SIGNAL",
                "trend": "SHORT",
                "score": 95,
                "threshold": 55,
                "would_route": "LIMIT",
                "signal_plan": {
                    "route": "LIMIT",
                    "order_type": "Limit",
                    "execution_entry": 0.15405,
                    "stop_loss": 0.1542,
                    "execution_tp": 0.15315,
                    "execution_rr": 6.0,
                    "protective_stop_loss": 0.154589175,
                    "protective_stop_adjusted": True,
                    "protective_execution_rr": 1.6692,
                    "route_reference_rr": 0.75,
                    "min_rr": 1.0,
                    "min_stop_pct": 0.35,
                    "zone_top": 0.1542,
                },
            }
        )

        self.assertEqual(compact["signal_plan"]["order_type"], "Limit")
        self.assertTrue(compact["signal_plan"]["protective_stop_adjusted"])
        self.assertNotIn("zone_top", compact["signal_plan"])

    def test_compact_cycle_removes_verbose_results(self):
        cycle = {
            "cycle": 1,
            "status": "OK",
            "duration_s": 1.2,
            "symbols_scanned": 1,
            "signals": 0,
            "waiting_setups": 0,
            "macro": {"BTC_trend": 1.0},
            "news": {"action": "NONE"},
            "results": [{"symbol": "BTCUSDT", "status": "FLAT", "trend": "FLAT"}],
        }

        compact = compact_cycle(cycle)

        self.assertNotIn("results", compact)
        self.assertEqual(compact["waiting_setups"], 0)
        self.assertEqual(compact["summary"]["status_counts"], {"FLAT": 1})


if __name__ == "__main__":
    unittest.main()
