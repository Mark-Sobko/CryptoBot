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
    news_score_bonus,
    parse_symbols,
    protective_stop_loss,
    risk_reward_ratio,
    summarize_cycle,
    summarize_cycles,
    validate_market_data,
    validate_read_only_environment,
)


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
                        "poi": {"reason": "smc_not_ok", "type": "FVG"},
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
        self.assertEqual(counts["poi_type_counts"], {"FVG": 1, "missing": 1})
        self.assertEqual(counts["m5_trigger_counts"], {"false": 1})
        self.assertEqual(counts["pd_alignment_counts"], {"false": 2})
        self.assertEqual(counts["liquidity_target_counts"], {"missing": 2})

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
            summary["reject_reasons"],
            {"market_filter": 1, "score_below_threshold:0/55": 1},
        )
        self.assertEqual(len(summary["near_setups"]), 1)
        self.assertEqual(summary["near_setups"][0]["symbol"], "ETHUSDT")
        self.assertEqual(
            summary["near_setups"][0]["failed_checks"],
            ["poi", "m5", "pd_alignment", "liquidity_target"],
        )
        self.assertEqual(
            summary["near_setups"][0]["blocker_details"]["poi"]["reason"],
            "missing",
        )
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
        self.assertEqual(len(summary["near_setups"]), 1)
        self.assertEqual(summary["near_setup_counts"], {"RENDERUSDT": 2})
        self.assertEqual(summary["near_setup_failed_check_counts"], {"m5": 2, "poi": 2})
        self.assertEqual(
            summary["blocker_detail_counts"]["poi_reason_counts"],
            {"missing": 2},
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
