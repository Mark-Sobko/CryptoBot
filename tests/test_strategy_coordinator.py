import os
import unittest

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from engine.market_regime import REGIME_LOW_VOL_COMPRESSION, REGIME_RANGE
from engine.strategy_coordinator import (
    DECISION_CONFLICT,
    DECISION_NO_ACTION,
    DECISION_WATCH_ONLY,
    ReadOnlyStrategyCoordinator,
)


def _candidate(**overrides):
    candidate = {
        "strategy": "MEAN_REVERSION",
        "status": "WATCH_ONLY",
        "side": "SHORT",
        "score": 80,
        "threshold": 70,
        "rr": 1.6,
        "order_type": "Limit",
        "entry": 101.0,
        "stop_loss": 102.0,
        "target": 99.0,
        "reason": "",
    }
    candidate.update(overrides)
    return candidate


class StrategyCoordinatorTests(unittest.TestCase):
    def test_prioritizes_native_range_strategy_over_higher_score_generic_candidate(self):
        coordinator = ReadOnlyStrategyCoordinator()

        decision = coordinator.decide(
            symbol="OPUSDT",
            regime_result={"regime": REGIME_RANGE},
            strategy_results=[
                _candidate(strategy="MEAN_REVERSION", score=82, rr=1.4),
                _candidate(strategy="VOLATILITY_EXPANSION", score=96, rr=2.4),
            ],
        )

        self.assertEqual(decision["decision"], DECISION_WATCH_ONLY)
        self.assertEqual(decision["selected_strategy"], "MEAN_REVERSION")
        self.assertEqual(decision["reason"], "selected_highest_priority_candidate")
        self.assertEqual(decision["candidate_count"], 2)

    def test_compression_prefers_breakout_over_volatility_expansion(self):
        coordinator = ReadOnlyStrategyCoordinator()

        decision = coordinator.decide(
            symbol="WIFUSDT",
            regime_result={"regime": REGIME_LOW_VOL_COMPRESSION},
            strategy_results=[
                _candidate(strategy="BREAKOUT", side="LONG", score=78, threshold=75, entry=100.0, stop_loss=98.0, target=104.0),
                _candidate(strategy="VOLATILITY_EXPANSION", side="LONG", score=92, threshold=80, entry=100.0, stop_loss=98.0, target=105.0),
            ],
        )

        self.assertEqual(decision["decision"], DECISION_WATCH_ONLY)
        self.assertEqual(decision["selected_strategy"], "BREAKOUT")

    def test_rejects_watch_candidate_with_invalid_directional_plan(self):
        coordinator = ReadOnlyStrategyCoordinator()

        decision = coordinator.decide(
            symbol="BTCUSDT",
            regime_result={"regime": REGIME_RANGE},
            strategy_results=[
                _candidate(side="LONG", entry=100.0, stop_loss=101.0, target=105.0),
            ],
        )

        self.assertEqual(decision["decision"], DECISION_NO_ACTION)
        self.assertEqual(decision["reason"], "no_valid_strategy_candidate")
        self.assertEqual(decision["rejected_candidate_count"], 1)
        self.assertEqual(
            decision["rejected_candidates"][0]["coordinator_rejection"],
            "invalid_directional_plan",
        )

    def test_blocks_conflicting_valid_candidate_sides_with_context(self):
        coordinator = ReadOnlyStrategyCoordinator()

        decision = coordinator.decide(
            symbol="BTCUSDT",
            regime_result={"regime": REGIME_RANGE},
            strategy_results=[
                _candidate(strategy="MEAN_REVERSION", side="SHORT", score=85),
                _candidate(
                    strategy="VOLATILITY_EXPANSION",
                    side="LONG",
                    score=90,
                    entry=100.0,
                    stop_loss=98.0,
                    target=105.0,
                ),
            ],
        )

        self.assertEqual(decision["decision"], DECISION_CONFLICT)
        self.assertEqual(decision["candidate_count"], 2)
        self.assertEqual(decision["candidate_sides"], ["LONG", "SHORT"])
        self.assertEqual(
            decision["candidate_strategies"],
            ["MEAN_REVERSION", "VOLATILITY_EXPANSION"],
        )


if __name__ == "__main__":
    unittest.main()
