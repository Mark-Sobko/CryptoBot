import json
import os
from pathlib import Path
import tempfile
import unittest

import pandas as pd

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from engine.market_regime import (
    REGIME_CHOP,
    REGIME_DATA_ERROR,
    REGIME_LOW_VOL_COMPRESSION,
    REGIME_RANGE,
    REGIME_TRENDING,
    build_regime_setup,
    calculate_regime_metrics,
    classify_regime_metrics,
)
from scripts.run_market_regime_observer import (
    append_jsonl_file,
    build_cycle_error,
    build_run_output,
    compact_result,
    RUN_STATUS_COMPLETED_WITH_ERRORS,
    summarize_results,
    summarize_cycles,
    write_json_file,
)


class MarketRegimeObserverTests(unittest.TestCase):
    @staticmethod
    def _base_metrics(**overrides):
        metrics = {
            "ok": True,
            "price": 100.0,
            "adx": 16.0,
            "atr_pct": 1.0,
            "atr_percentile": 0.5,
            "bb_width_pct": 4.0,
            "bb_width_percentile": 0.5,
            "efficiency_ratio": 0.15,
            "relative_volume": 0.6,
            "range_high": 102.0,
            "range_low": 98.0,
            "range_mid": 100.0,
            "range_width_pct": 4.0,
            "range_position": 0.5,
            "upper_touches": 3,
            "lower_touches": 3,
        }
        metrics.update(overrides)
        return metrics

    def test_classifies_trending_without_creating_alt_setup(self):
        metrics = self._base_metrics(
            adx=30.0,
            efficiency_ratio=0.42,
            atr_percentile=0.55,
            range_position=0.9,
        )

        classification = classify_regime_metrics(metrics)
        setup = build_regime_setup(metrics, classification)

        self.assertEqual(classification["regime"], REGIME_TRENDING)
        self.assertEqual(classification["trade_posture"], "USE_SMC")
        self.assertEqual(setup["status"], "SMC_ONLY")

    def test_classifies_range_but_only_watches_edges(self):
        metrics = self._base_metrics(adx=14.0, efficiency_ratio=0.09, range_position=0.91)

        classification = classify_regime_metrics(metrics)
        setup = build_regime_setup(metrics, classification)

        self.assertEqual(classification["regime"], REGIME_RANGE)
        self.assertEqual(classification["trade_posture"], "EDGE_ONLY")
        self.assertEqual(setup["status"], "RANGE_EDGE_WATCH")
        self.assertEqual(setup["side"], "SHORT")
        self.assertIn("liquidity_sweep_or_reclaim", setup["requires"])

        middle_metrics = dict(metrics, range_position=0.5)
        middle_setup = build_regime_setup(middle_metrics, classification)

        self.assertEqual(middle_setup["status"], "RANGE_MID_NO_TRADE")

    def test_classifies_low_volatility_compression_as_wait_breakout(self):
        metrics = self._base_metrics(
            adx=11.0,
            atr_percentile=0.08,
            bb_width_percentile=0.12,
            efficiency_ratio=0.08,
            relative_volume=0.7,
        )

        classification = classify_regime_metrics(metrics)
        setup = build_regime_setup(metrics, classification)

        self.assertEqual(classification["regime"], REGIME_LOW_VOL_COMPRESSION)
        self.assertEqual(classification["trade_posture"], "WAIT_BREAKOUT")
        self.assertEqual(setup["status"], "WAIT_BREAKOUT")
        self.assertEqual(setup["confirmation_required"], ["range_break", "volume_expansion", "retest"])

    def test_classifies_chop_when_range_structure_is_missing(self):
        metrics = self._base_metrics(
            adx=16.0,
            efficiency_ratio=0.12,
            upper_touches=1,
            lower_touches=1,
        )

        classification = classify_regime_metrics(metrics)
        setup = build_regime_setup(metrics, classification)

        self.assertEqual(classification["regime"], REGIME_CHOP)
        self.assertEqual(classification["trade_posture"], "NO_TRADE")
        self.assertIsNone(setup)

    def test_invalid_metrics_are_data_error(self):
        classification = classify_regime_metrics({"ok": False, "reason": "invalid_1h_data"})

        self.assertEqual(classification["regime"], REGIME_DATA_ERROR)
        self.assertEqual(classification["trade_posture"], "NO_TRADE")
        self.assertEqual(classification["reason"], "invalid_1h_data")

    def test_calculate_regime_metrics_reports_invalid_data(self):
        metrics = calculate_regime_metrics(pd.DataFrame(), pd.DataFrame())

        self.assertFalse(metrics["ok"])
        self.assertEqual(metrics["reason"], "invalid_1h_data")

    def test_summary_counts_watches_without_treating_chop_as_signal(self):
        results = [
            {
                "symbol": "OPUSDT",
                "status": "RANGE_EDGE_WATCH",
                "regime": REGIME_RANGE,
                "confidence": 76,
                "trade_posture": "EDGE_ONLY",
                "reason": "bounded_range_with_repeated_edges",
                "metrics": self._base_metrics(range_position=0.92),
                "setup": {"status": "RANGE_EDGE_WATCH", "side": "SHORT"},
            },
            {
                "symbol": "SOLUSDT",
                "status": "SMC_ONLY",
                "regime": REGIME_TRENDING,
                "confidence": 84,
                "trade_posture": "USE_SMC",
                "reason": "adx_efficiency_and_volatility_confirmed",
                "metrics": self._base_metrics(adx=28.0, er=0.42),
                "trend_pullback": {
                    "strategy": "TREND_PULLBACK",
                    "status": "WATCH_ONLY",
                    "score": 88,
                    "threshold": 78,
                    "side": "LONG",
                    "order_type": "Limit",
                    "entry": 100.2,
                    "stop_loss": 98.8,
                    "target": 103.4,
                    "rr": 2.2857,
                },
            },
            {
                "symbol": "BTCUSDT",
                "status": "NO_TRADE",
                "regime": REGIME_CHOP,
                "confidence": 50,
                "trade_posture": "NO_TRADE",
                "reason": "no_high_quality_trend_range_or_compression",
                "metrics": self._base_metrics(upper_touches=1, lower_touches=1),
                "volatility_expansion": {
                    "strategy": "VOLATILITY_EXPANSION",
                    "status": "WATCH_ONLY",
                    "score": 86,
                    "threshold": 80,
                    "side": "SHORT",
                    "order_type": "Limit",
                    "entry": 99.8,
                    "stop_loss": 101.0,
                    "target": 97.8,
                    "rr": 1.6667,
                },
            },
        ]

        summary = summarize_results(results)
        compact = compact_result(results[0])

        self.assertEqual(summary["watch_total"], 1)
        self.assertEqual(summary["range_edge_watch_total"], 1)
        self.assertEqual(summary["regime_counts"][REGIME_CHOP], 1)
        self.assertEqual(summary["trend_pullback_watch_total"], 1)
        self.assertEqual(summary["volatility_expansion_watch_total"], 1)
        self.assertEqual(summary["trend_pullback_status_counts"]["WATCH_ONLY"], 1)
        self.assertEqual(summary["volatility_expansion_status_counts"]["WATCH_ONLY"], 1)
        self.assertEqual(compact["setup"]["side"], "SHORT")

    def test_summarize_cycles_counts_trend_pullback_watch_cycles(self):
        cycles = [
            {
                "cycle": 1,
                "results": [
                    {
                        "symbol": "SOLUSDT",
                        "status": "SMC_ONLY",
                        "regime": REGIME_TRENDING,
                        "confidence": 84,
                        "trade_posture": "USE_SMC",
                        "trend_pullback": {
                            "strategy": "TREND_PULLBACK",
                            "status": "WATCH_ONLY",
                            "score": 88,
                            "threshold": 78,
                            "side": "LONG",
                        },
                    }
                ],
                "summary": {"trend_pullback_watch_total": 1},
            }
        ]

        summary = summarize_cycles(cycles)

        self.assertEqual(summary["trend_pullback_watch_total"], 1)
        self.assertEqual(summary["cycles_with_trend_pullback_watch"], 1)

    def test_summarize_cycles_counts_volatility_expansion_watch_cycles(self):
        cycles = [
            {
                "cycle": 1,
                "results": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "NO_TRADE",
                        "regime": REGIME_CHOP,
                        "confidence": 64,
                        "trade_posture": "NO_TRADE",
                        "volatility_expansion": {
                            "strategy": "VOLATILITY_EXPANSION",
                            "status": "WATCH_ONLY",
                            "score": 86,
                            "threshold": 80,
                            "side": "LONG",
                        },
                    }
                ],
                "summary": {"volatility_expansion_watch_total": 1},
            }
        ]

        summary = summarize_cycles(cycles)

        self.assertEqual(summary["volatility_expansion_watch_total"], 1)
        self.assertEqual(summary["cycles_with_volatility_expansion_watch"], 1)

    def test_writes_progress_jsonl_and_final_json_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_path = Path(tmpdir) / "nested" / "progress.jsonl"
            final_path = Path(tmpdir) / "nested" / "final.json"

            append_jsonl_file(str(progress_path), {"cycle": 1, "status": "OK"})
            append_jsonl_file(str(progress_path), {"cycle": 2, "status": "OK"})
            write_json_file(str(final_path), {"status": "OK", "cycles_completed": 2})

            progress_lines = progress_path.read_text().strip().splitlines()
            final_payload = json.loads(final_path.read_text())

            self.assertEqual(len(progress_lines), 2)
            self.assertEqual(json.loads(progress_lines[0])["cycle"], 1)
            self.assertEqual(json.loads(progress_lines[1])["cycle"], 2)
            self.assertEqual(final_payload["cycles_completed"], 2)

    def test_cycle_error_keeps_unattended_run_summarizable(self):
        cycle = build_cycle_error(["BTCUSDT", "ETHUSDT"], RuntimeError("api timeout"))
        cycle["cycle"] = 1
        cycle["duration_s"] = 0.01

        summary = summarize_cycles([cycle])
        output = build_run_output(
            status=RUN_STATUS_COMPLETED_WITH_ERRORS,
            cycles_requested=3,
            symbols=["BTCUSDT", "ETHUSDT"],
            cycles=[cycle],
            summary_only=True,
        )

        self.assertEqual(cycle["status"], "CYCLE_ERROR")
        self.assertEqual(summary["cycle_errors_total"], 1)
        self.assertEqual(summary["errors_total"], 2)
        self.assertEqual(output["status"], RUN_STATUS_COMPLETED_WITH_ERRORS)
        self.assertEqual(output["cycles_completed"], 1)
        self.assertEqual(output["cycles"][0]["cycle_error"]["symbols_requested"], 2)

    def test_write_json_file_replaces_existing_checkpoint_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checkpoint_path = Path(tmpdir) / "checkpoint.json"

            write_json_file(str(checkpoint_path), {"status": "RUNNING", "cycles_completed": 1})
            write_json_file(str(checkpoint_path), {"status": "OK", "cycles_completed": 2})

            payload = json.loads(checkpoint_path.read_text())
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload["cycles_completed"], 2)


if __name__ == "__main__":
    unittest.main()
