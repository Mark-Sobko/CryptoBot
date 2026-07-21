import os
import unittest

import pandas as pd

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from scripts.run_strategy_observer import (
    compact_cycle,
    news_score_bonus,
    parse_symbols,
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
                    },
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
            summary["near_setups"][0]["blockers"],
            ["poi", "m5", "pd_alignment", "liquidity_target"],
        )

    def test_summarize_cycles_deduplicates_near_setups(self):
        cycle = {
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

        summary = summarize_cycles([cycle, cycle])

        self.assertEqual(summary["status_counts"], {"REJECT": 2})
        self.assertEqual(summary["signals_total"], 0)
        self.assertEqual(summary["errors_total"], 0)
        self.assertEqual(len(summary["near_setups"]), 1)
        self.assertEqual(summary["near_setup_counts"], {"RENDERUSDT": 2})
        self.assertEqual(summary["near_setup_blocker_counts"], {"m5": 2, "poi": 2})

    def test_compact_cycle_removes_verbose_results(self):
        cycle = {
            "cycle": 1,
            "status": "OK",
            "duration_s": 1.2,
            "symbols_scanned": 1,
            "signals": 0,
            "macro": {"BTC_trend": 1.0},
            "news": {"action": "NONE"},
            "results": [{"symbol": "BTCUSDT", "status": "FLAT", "trend": "FLAT"}],
        }

        compact = compact_cycle(cycle)

        self.assertNotIn("results", compact)
        self.assertEqual(compact["summary"]["status_counts"], {"FLAT": 1})


if __name__ == "__main__":
    unittest.main()
