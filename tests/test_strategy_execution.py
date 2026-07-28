import os
import unittest

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from engine.strategy_execution import (
    ALT_EXECUTION_READY,
    ALT_EXECUTION_REJECTED,
    build_single_target_tp_levels,
    build_strategy_execution_plan,
    build_strategy_poi,
    parse_allowed_strategies,
)


def _decision(**overrides):
    decision = {
        "symbol": "WIFUSDT",
        "decision": "WATCH_ONLY",
        "selected_strategy": "MEAN_REVERSION",
        "side": "SHORT",
        "score": 82,
        "threshold": 70,
        "plan": {
            "order_type": "Limit",
            "entry": 0.154,
            "stop_loss": 0.156,
            "target": 0.148,
            "rr": 3.0,
        },
    }
    decision.update(overrides)
    return decision


class StrategyExecutionTests(unittest.TestCase):
    def test_parse_allowed_strategies_defaults_to_supported_set(self):
        allowed = parse_allowed_strategies("")

        self.assertIn("MEAN_REVERSION", allowed)
        self.assertIn("BREAKOUT", allowed)
        self.assertIn("TREND_PULLBACK", allowed)
        self.assertIn("VOLATILITY_EXPANSION", allowed)

    def test_build_strategy_execution_plan_accepts_valid_watch_decision(self):
        plan = build_strategy_execution_plan(
            _decision(),
            allowed_strategies="MEAN_REVERSION,BREAKOUT",
            min_rr=1.2,
        )

        self.assertEqual(plan["status"], ALT_EXECUTION_READY)
        self.assertEqual(plan["strategy"], "MEAN_REVERSION")
        self.assertEqual(plan["side"], "SHORT")
        self.assertEqual(plan["order_type"], "Limit")
        self.assertAlmostEqual(plan["target"], 0.148)

    def test_build_strategy_execution_plan_rejects_non_watch_decision(self):
        plan = build_strategy_execution_plan(_decision(decision="NO_ACTION"))

        self.assertEqual(plan["status"], ALT_EXECUTION_REJECTED)
        self.assertEqual(plan["reason"], "decision_not_watch_only")

    def test_build_strategy_execution_plan_rejects_disallowed_strategy(self):
        plan = build_strategy_execution_plan(
            _decision(selected_strategy="BREAKOUT"),
            allowed_strategies={"MEAN_REVERSION"},
        )

        self.assertEqual(plan["status"], ALT_EXECUTION_REJECTED)
        self.assertEqual(plan["reason"], "strategy_not_allowed")

    def test_build_strategy_execution_plan_rejects_bad_rr_and_direction(self):
        low_rr = _decision(plan={"order_type": "Limit", "entry": 100.0, "stop_loss": 102.0, "target": 99.5, "rr": 0.25})
        bad_direction = _decision(side="LONG", plan={"order_type": "Limit", "entry": 100.0, "stop_loss": 101.0, "target": 105.0, "rr": 2.0})

        self.assertEqual(
            build_strategy_execution_plan(low_rr, min_rr=1.2)["reason"],
            "rr_below_execution_min",
        )
        self.assertEqual(
            build_strategy_execution_plan(bad_direction, min_rr=1.2)["reason"],
            "invalid_directional_plan",
        )

    def test_poi_and_single_target_helpers_use_strategy_plan_values(self):
        plan = build_strategy_execution_plan(_decision(), min_rr=1.2)
        poi = build_strategy_poi(plan)
        tp_levels = build_single_target_tp_levels(plan)

        self.assertEqual(poi["type"], "ALT_MEAN_REVERSION")
        self.assertEqual(poi["side"], "SHORT")
        self.assertAlmostEqual(poi["mid"], 0.154)
        self.assertEqual(tp_levels, {"tp1": 0.148})


if __name__ == "__main__":
    unittest.main()
