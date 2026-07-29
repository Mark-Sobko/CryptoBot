import datetime as dt
import unittest

from engine.strategy_health import (
    HEALTH_BLOCKED,
    HEALTH_OK,
    evaluate_strategy_execution_health,
    is_unhealthy_rejection,
)


class StrategyHealthTests(unittest.TestCase):
    def test_unhealthy_rejection_classification_ignores_normal_guards(self):
        self.assertFalse(is_unhealthy_rejection("strategy_cooldown"))
        self.assertFalse(is_unhealthy_rejection("max_positions_reached"))
        self.assertFalse(is_unhealthy_rejection("strategy_health_guard:rejection_streak"))

        self.assertTrue(is_unhealthy_rejection("executor_failed"))
        self.assertTrue(is_unhealthy_rejection("risk_reject:active_position_without_stop"))
        self.assertTrue(is_unhealthy_rejection("zero_qty_after_notional_cap"))

    def test_blocks_after_executor_failure_in_window(self):
        now = dt.datetime(2026, 7, 29, tzinfo=dt.timezone.utc)
        health = evaluate_strategy_execution_health(
            [
                {
                    "ts": now - dt.timedelta(minutes=5),
                    "strategy": "MEAN_REVERSION",
                    "symbol": "WIFUSDT",
                    "reason": "executor_failed",
                    "order_submitted": False,
                }
            ],
            strategy="MEAN_REVERSION",
            now=now,
            max_executor_failures=1,
        )

        self.assertEqual(health["status"], HEALTH_BLOCKED)
        self.assertEqual(health["reason"], "strategy_health_guard:executor_failures")

    def test_blocks_after_consecutive_unhealthy_rejections(self):
        now = dt.datetime(2026, 7, 29, tzinfo=dt.timezone.utc)
        events = [
            {
                "ts": now - dt.timedelta(minutes=3),
                "strategy": "BREAKOUT",
                "symbol": "BTCUSDT",
                "reason": "risk_reject:no_stop",
                "order_submitted": False,
            },
            {
                "ts": now - dt.timedelta(minutes=2),
                "strategy": "BREAKOUT",
                "symbol": "ETHUSDT",
                "reason": "zero_qty_after_risk_sizing",
                "order_submitted": False,
            },
        ]

        health = evaluate_strategy_execution_health(
            events,
            strategy="BREAKOUT",
            now=now,
            max_recent_rejections=2,
            max_executor_failures=0,
        )

        self.assertEqual(health["status"], HEALTH_BLOCKED)
        self.assertEqual(health["reason"], "strategy_health_guard:rejection_streak")

    def test_successful_submit_resets_rejection_streak(self):
        now = dt.datetime(2026, 7, 29, tzinfo=dt.timezone.utc)
        events = [
            {
                "ts": now - dt.timedelta(minutes=4),
                "strategy": "TREND_PULLBACK",
                "reason": "risk_reject:no_stop",
                "order_submitted": False,
            },
            {
                "ts": now - dt.timedelta(minutes=3),
                "strategy": "TREND_PULLBACK",
                "reason": "submitted",
                "order_submitted": True,
            },
            {
                "ts": now - dt.timedelta(minutes=2),
                "strategy": "TREND_PULLBACK",
                "reason": "zero_qty_after_notional_cap",
                "order_submitted": False,
            },
        ]

        health = evaluate_strategy_execution_health(
            events,
            strategy="TREND_PULLBACK",
            now=now,
            max_recent_rejections=2,
            max_executor_failures=0,
        )

        self.assertEqual(health["status"], HEALTH_OK)
        self.assertEqual(health["consecutive_unhealthy_rejections"], 1)


if __name__ == "__main__":
    unittest.main()
