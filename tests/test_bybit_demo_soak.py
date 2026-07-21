import argparse
import unittest

from scripts.run_bybit_demo_lifecycle_soak import (
    build_lifecycle_command,
    parse_json_objects,
    validate_lifecycle_summary,
)


class BybitDemoSoakTests(unittest.TestCase):
    def test_parse_json_objects_ignores_noise_and_returns_dicts(self):
        objects = parse_json_objects('noise {"status":"OK"} more {"cleanup": {"x": 1}}')

        self.assertEqual(objects, [{"status": "OK"}, {"cleanup": {"x": 1}}])

    def test_validate_lifecycle_summary_accepts_required_steps(self):
        summary = {
            "status": "OK",
            "steps": [
                {"name": "limit_create"},
                {"name": "limit_amend"},
                {"name": "retcode_matrix"},
                {"name": "limit_cancel"},
                {"name": "market_open"},
                {"name": "partial_reduce_only_close"},
                {"name": "reduce_only_tp_create"},
                {"name": "stop_loss_set"},
                {"name": "restart_recovery_sync", "status": "OPEN"},
                {"name": "reduce_only_tp_cancel"},
                {"name": "stop_loss_clear"},
                {"name": "market_close_reduce_only"},
            ],
        }

        self.assertEqual(validate_lifecycle_summary(summary, require_partial_close=True), [])

    def test_validate_lifecycle_summary_reports_missing_recovery(self):
        summary = {"status": "OK", "steps": [{"name": "limit_create"}]}

        errors = validate_lifecycle_summary(summary, require_partial_close=True)

        self.assertTrue(any("missing_steps" in error for error in errors))
        self.assertIn("restart_recovery_sync missing", errors)

    def test_build_lifecycle_command_defaults_to_skip_partial_fill_probe(self):
        args = argparse.Namespace(
            symbol="XRPUSDT",
            max_notional="25",
            wait=20.0,
            skip_partial_fill_probe=True,
            skip_partial_close=False,
        )

        command = build_lifecycle_command(args)

        self.assertIn("scripts/run_bybit_demo_lifecycle.py", command[1])
        self.assertIn("--skip-partial-fill-probe", command)
        self.assertNotIn("--skip-partial-close", command)


if __name__ == "__main__":
    unittest.main()
