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


if __name__ == "__main__":
    unittest.main()
