import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.strategy_journal import StrategyObservationJournal
from scripts.summarize_strategy_journal import load_jsonl, summarize_entries


class StrategyObservationJournalTests(unittest.TestCase):
    def test_disabled_journal_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "strategy.jsonl"
            journal = StrategyObservationJournal(path, enabled=False)

            self.assertFalse(journal.record("ALT_STRATEGY_DECISION", "BTCUSDT", {}))
            self.assertFalse(path.exists())

    def test_records_jsonl_and_summarizes_watch_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nested" / "strategy.jsonl"
            journal = StrategyObservationJournal(path)

            self.assertTrue(
                journal.record(
                    "ALT_STRATEGY_DECISION",
                    "WIFUSDT",
                    {
                        "decision": "WATCH_ONLY",
                        "selected_strategy": "MEAN_REVERSION",
                        "regime": "RANGE",
                        "side": "SHORT",
                        "score": 85,
                        "threshold": 55,
                        "reason": "single_read_only_candidate",
                        "plan": {"rr": Decimal("2.5"), "entry": 1.0},
                    },
                )
            )

            raw_entry = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(raw_entry["event_type"], "ALT_STRATEGY_DECISION")
            self.assertEqual(raw_entry["symbol"], "WIFUSDT")
            self.assertEqual(raw_entry["data"]["plan"]["rr"], 2.5)

            entries, errors = load_jsonl(path)
            summary = summarize_entries(entries)

            self.assertEqual(errors, [])
            self.assertEqual(summary["entries_total"], 1)
            self.assertEqual(summary["watch_total"], 1)
            self.assertEqual(summary["decision_counts"]["WATCH_ONLY"], 1)
            self.assertEqual(summary["strategy_counts"]["MEAN_REVERSION"], 1)
            self.assertEqual(summary["symbol_counts"]["WIFUSDT"], 1)
            self.assertEqual(summary["recent_watch"][0]["plan"]["rr"], 2.5)

    def test_summary_reports_alt_execution_policy_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "strategy.jsonl"
            journal = StrategyObservationJournal(path)

            self.assertTrue(
                journal.record(
                    "ALT_STRATEGY_EXECUTION",
                    "WIFUSDT",
                    {
                        "strategy": "MEAN_REVERSION",
                        "side": "SHORT",
                        "score": 82,
                        "threshold": 70,
                        "reason": "submitted",
                        "order_submitted": True,
                        "policy_min_rr": 1.2,
                        "max_notional_usd": 25.0,
                        "risk_pct_multiplier": 0.5,
                        "cooldown_minutes": 60.0,
                        "max_hold_minutes": 120.0,
                    },
                )
            )
            self.assertTrue(
                journal.record(
                    "ALT_STRATEGY_EXECUTION",
                    "WIFUSDT",
                    {
                        "strategy": "MEAN_REVERSION",
                        "side": "SHORT",
                        "reason": "strategy_cooldown",
                        "order_submitted": False,
                    },
                )
            )

            entries, errors = load_jsonl(path)
            summary = summarize_entries(entries)

            self.assertEqual(errors, [])
            self.assertEqual(summary["execution_total"], 2)
            self.assertEqual(summary["execution_submitted_counts"]["MEAN_REVERSION"], 1)
            self.assertEqual(summary["execution_rejected_counts"]["MEAN_REVERSION"], 1)
            self.assertEqual(summary["execution_reason_counts"]["strategy_cooldown"], 1)
            self.assertEqual(summary["recent_executions"][0]["risk_pct_multiplier"], 0.5)
            self.assertEqual(summary["recent_executions"][0]["max_hold_minutes"], 120.0)

    def test_load_jsonl_reports_parse_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "strategy.jsonl"
            path.write_text('{"ok": true}\nnot-json\n[]\n', encoding="utf-8")

            entries, errors = load_jsonl(path)

            self.assertEqual(len(entries), 1)
            self.assertEqual(len(errors), 2)
