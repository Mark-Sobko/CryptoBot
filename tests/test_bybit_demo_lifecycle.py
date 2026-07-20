import os
import unittest
from decimal import Decimal


os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")


class BybitDemoLifecycleHelperTests(unittest.TestCase):
    def test_choose_qty_reserves_enough_for_partial_close(self):
        from scripts.run_bybit_demo_lifecycle import choose_qty

        qty = choose_qty(
            last_price=Decimal("1.00"),
            min_order_price=Decimal("0.80"),
            instrument={
                "min_qty": Decimal("1"),
                "qty_step": Decimal("0.1"),
                "min_notional": Decimal("5"),
            },
            max_notional=Decimal("15"),
            require_partial_close=True,
        )

        self.assertGreaterEqual(qty * Decimal("1.00"), Decimal("10.5"))
        self.assertLessEqual(qty * Decimal("1.00"), Decimal("15"))

    def test_choose_qty_fails_when_min_notional_exceeds_limit(self):
        from scripts.run_bybit_demo_lifecycle import LifecycleError, choose_qty

        with self.assertRaises(LifecycleError):
            choose_qty(
                last_price=Decimal("1.00"),
                min_order_price=Decimal("0.80"),
                instrument={
                    "min_qty": Decimal("1"),
                    "qty_step": Decimal("0.1"),
                    "min_notional": Decimal("5"),
                },
                max_notional=Decimal("3"),
                require_partial_close=True,
            )

    def test_summarize_failure_keeps_retcode_context(self):
        from scripts.run_bybit_demo_lifecycle import summarize_failure

        summary = summarize_failure({"retCode": 10001, "retMsg": "position idx not match position mode"})

        self.assertEqual(summary["retCode"], 10001)
        self.assertIn("position idx", summary["retMsg"])

    def test_summarize_failure_truncates_exception_text(self):
        from scripts.run_bybit_demo_lifecycle import summarize_failure

        summary = summarize_failure("first line\nsecond line")

        self.assertEqual(summary["exception"], "first line")

    def test_partial_fill_order_plan_respects_max_notional(self):
        from scripts.run_bybit_demo_lifecycle import plan_partial_fill_order

        plan = plan_partial_fill_order(
            instrument={
                "min_qty": Decimal("1"),
                "qty_step": Decimal("0.1"),
                "min_notional": Decimal("5"),
            },
            ask_price=Decimal("1.00"),
            ask_size=Decimal("3"),
            max_notional=Decimal("10"),
        )

        self.assertTrue(plan["eligible"])
        self.assertEqual(plan["target_qty"], "9.5")
        self.assertEqual(plan["required_notional"], "9.5")

    def test_partial_fill_order_plan_rejects_large_top_ask(self):
        from scripts.run_bybit_demo_lifecycle import plan_partial_fill_order

        plan = plan_partial_fill_order(
            instrument={
                "min_qty": Decimal("1"),
                "qty_step": Decimal("0.1"),
                "min_notional": Decimal("5"),
            },
            ask_price=Decimal("2.00"),
            ask_size=Decimal("20"),
            max_notional=Decimal("10"),
        )

        self.assertFalse(plan["eligible"])
        self.assertEqual(plan["reason"], "top_ask_too_large")

    def test_partial_fill_order_plan_honors_target_notional_pct(self):
        from scripts.run_bybit_demo_lifecycle import plan_partial_fill_order

        plan = plan_partial_fill_order(
            instrument={
                "min_qty": Decimal("1"),
                "qty_step": Decimal("0.1"),
                "min_notional": Decimal("5"),
            },
            ask_price=Decimal("1.00"),
            ask_size=Decimal("3"),
            max_notional=Decimal("10"),
            target_notional_pct=Decimal("0.50"),
        )

        self.assertTrue(plan["eligible"])
        self.assertEqual(plan["target_qty"], "5")
        self.assertEqual(plan["required_notional"], "5")

    def test_parse_symbol_csv_deduplicates_and_normalizes(self):
        from scripts.run_bybit_demo_lifecycle import parse_symbol_csv, unique_symbols

        self.assertEqual(parse_symbol_csv(" opUSDT, OPUSDT, xrpUsdt "), ["OPUSDT", "XRPUSDT"])
        self.assertEqual(unique_symbols(["btcusdt", "BTCUSDT", "", "ethusdt"]), ["BTCUSDT", "ETHUSDT"])


if __name__ == "__main__":
    unittest.main()
