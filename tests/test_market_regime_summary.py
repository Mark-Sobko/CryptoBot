import json
from pathlib import Path
import tempfile
import unittest

from scripts.summarize_market_regime_observer import (
    load_observer_payload,
    summarize_payload,
)


class MarketRegimeSummaryTests(unittest.TestCase):
    @staticmethod
    def _cycle():
        return {
            "cycle": 1,
            "status": "OK",
            "symbols_scanned": 3,
            "results": [
                {
                    "symbol": "BTCUSDT",
                    "status": "SMC_ONLY",
                    "regime": "TRENDING",
                    "confidence": 74,
                    "trade_posture": "USE_SMC",
                    "mean_reversion": {"status": "DISABLED"},
                    "breakout": {"status": "DISABLED"},
                    "trend_pullback": {
                        "strategy": "TREND_PULLBACK",
                        "status": "WATCH_ONLY",
                        "score": 82,
                        "threshold": 78,
                        "side": "LONG",
                        "plan": {
                            "order_type": "Limit",
                            "entry": 100.5,
                            "stop_loss": 98.9,
                            "target": 104.0,
                            "rr": 2.18,
                        },
                    },
                    "volatility_expansion": {
                        "strategy": "VOLATILITY_EXPANSION",
                        "status": "WATCH_ONLY",
                        "score": 86,
                        "threshold": 80,
                        "side": "LONG",
                        "plan": {
                            "order_type": "Limit",
                            "entry": 101.1,
                            "stop_loss": 99.8,
                            "target": 103.3,
                            "rr": 1.69,
                        },
                    },
                    "decision": {"decision": "NO_ACTION"},
                },
                {
                    "symbol": "WIFUSDT",
                    "status": "WAIT_BREAKOUT",
                    "regime": "LOW_VOL_COMPRESSION",
                    "confidence": 82,
                    "trade_posture": "WAIT_BREAKOUT",
                    "mean_reversion": {"status": "DISABLED"},
                    "breakout": {
                        "strategy": "BREAKOUT",
                        "status": "WATCH_ONLY",
                        "score": 88,
                        "threshold": 75,
                        "side": "SHORT",
                        "plan": {
                            "order_type": "Limit",
                            "entry": 0.15,
                            "stop_loss": 0.153,
                            "target": 0.144,
                            "rr": 2.0,
                        },
                    },
                    "trend_pullback": {"status": "DISABLED"},
                    "volatility_expansion": {"status": "DISABLED"},
                    "decision": {
                        "decision": "WATCH_ONLY",
                        "selected_strategy": "BREAKOUT",
                        "side": "SHORT",
                        "score": 88,
                    },
                },
                {
                    "symbol": "SEIUSDT",
                    "status": "WAIT_BREAKOUT",
                    "regime": "LOW_VOL_COMPRESSION",
                    "confidence": 44,
                    "trade_posture": "WAIT_BREAKOUT",
                    "mean_reversion": {"status": "DISABLED"},
                    "breakout": {
                        "strategy": "BREAKOUT",
                        "status": "WAIT_BREAKOUT",
                        "score": 0,
                        "threshold": 75,
                        "side": None,
                        "failed_checks": ["range_break"],
                        "reason": "waiting_for_close_outside_compression_range",
                    },
                    "trend_pullback": {"status": "DISABLED"},
                    "volatility_expansion": {
                        "strategy": "VOLATILITY_EXPANSION",
                        "status": "WAIT_EXPANSION",
                        "score": 0,
                        "threshold": 80,
                        "side": None,
                        "failed_checks": ["volume_expansion", "impulse_body"],
                        "reason": "waiting_for_clean_volatility_expansion",
                    },
                    "decision": {"decision": "NO_ACTION"},
                },
            ],
        }

    def test_summarizes_observer_json_payload(self):
        payload = {
            "status": "OK",
            "cycles_requested": 1,
            "cycles_completed": 1,
            "cycles": [self._cycle()],
        }

        summary = summarize_payload(payload, top=4)

        self.assertEqual(summary["regime_counts"]["TRENDING"], 1)
        self.assertEqual(summary["breakout_watch_total"], 1)
        self.assertEqual(summary["trend_pullback_watch_total"], 1)
        self.assertEqual(summary["volatility_expansion_watch_total"], 1)
        self.assertEqual(summary["coordinator_watch_total"], 1)
        self.assertEqual(summary["recommendation"]["status"], "REVIEW_COORDINATED_WATCHES")
        self.assertEqual(summary["top_breakout_watches"][0]["symbol"], "WIFUSDT")
        self.assertEqual(summary["top_trend_pullback_watches"][0]["symbol"], "BTCUSDT")
        self.assertEqual(summary["top_volatility_expansion_watches"][0]["symbol"], "BTCUSDT")
        self.assertEqual(summary["strategy_failed_check_counts"]["breakout"]["range_break"], 1)
        self.assertEqual(summary["strategy_failed_check_counts"]["volatility_expansion"]["volume_expansion"], 1)
        self.assertEqual(
            summary["strategy_reason_counts"]["volatility_expansion"]["waiting_for_clean_volatility_expansion"],
            1,
        )
        self.assertIn(
            {
                "strategy": "breakout",
                "symbol": "SEIUSDT",
                "status": "WAIT_BREAKOUT",
                "count": 1,
            },
            summary["top_strategy_symbol_statuses"],
        )

    def test_loads_progress_jsonl_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_path = Path(tmpdir) / "progress.jsonl"
            record = {
                "type": "cycle",
                "cycle": 1,
                "cycles_requested": 12,
                "result": self._cycle(),
            }
            progress_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            payload = load_observer_payload(str(progress_path))
            summary = summarize_payload(payload, top=3)

            self.assertEqual(payload["status"], "PROGRESS_JSONL")
            self.assertEqual(payload["cycles_requested"], 12)
            self.assertEqual(summary["cycles_completed"], 1)
            self.assertEqual(summary["recommendation"]["status"], "REVIEW_COORDINATED_WATCHES")


if __name__ == "__main__":
    unittest.main()
