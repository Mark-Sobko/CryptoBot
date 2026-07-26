import os
import unittest

import pandas as pd

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from engine.market_regime import REGIME_RANGE, REGIME_TRENDING
from engine.strategies.mean_reversion import (
    STATUS_DISABLED,
    STATUS_REJECT_RR,
    STATUS_WAIT_RECLAIM,
    STATUS_WAIT_VOLUME,
    STATUS_WATCH_ONLY,
    MeanReversionStrategy,
)
from engine.strategy_coordinator import (
    DECISION_CONFLICT,
    DECISION_NO_ACTION,
    DECISION_WATCH_ONLY,
    ReadOnlyStrategyCoordinator,
)


class MeanReversionStrategyTests(unittest.TestCase):
    @staticmethod
    def _ohlcv(rows: list[dict[str, float]]) -> pd.DataFrame:
        return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])

    @staticmethod
    def _fifteen_min_data() -> pd.DataFrame:
        rows = []
        for index in range(45):
            base = 100.0 + (0.08 if index % 2 == 0 else -0.08)
            rows.append(
                {
                    "open": base,
                    "high": base + 0.45,
                    "low": base - 0.45,
                    "close": base + 0.02,
                    "volume": 1000.0,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _five_min_data(trigger: dict[str, float], *, volume: float = 700.0) -> pd.DataFrame:
        rows = []
        for index in range(35):
            base = 101.0 + (0.03 if index % 2 == 0 else -0.03)
            rows.append(
                {
                    "open": base,
                    "high": base + 0.12,
                    "low": base - 0.12,
                    "close": base,
                    "volume": volume,
                }
            )
        rows.append(trigger)
        rows.append(
            {
                "open": trigger["close"],
                "high": trigger["close"] + 0.01,
                "low": trigger["close"] - 0.01,
                "close": trigger["close"],
                "volume": 1.0,
            }
        )
        return pd.DataFrame(rows)

    @staticmethod
    def _range_result(**overrides):
        result = {
            "regime": REGIME_RANGE,
            "confidence": 90,
            "trade_posture": "EDGE_ONLY",
            "metrics": {
                "range_high": 102.0,
                "range_low": 98.0,
                "range_mid": 100.0,
                "range_position": 0.98,
            },
            "setup": {"status": "RANGE_EDGE_WATCH", "side": "SHORT"},
        }
        result.update(overrides)
        return result

    def test_disables_outside_range_regime(self):
        strategy = MeanReversionStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result={"regime": REGIME_TRENDING, "setup": {"status": "SMC_ONLY"}},
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                {"open": 101.9, "high": 102.1, "low": 101.2, "close": 101.5, "volume": 700.0}
            ),
        )

        self.assertEqual(result["status"], STATUS_DISABLED)
        self.assertEqual(result["failed_checks"], ["regime"])

    def test_short_range_edge_returns_read_only_watch_candidate(self):
        strategy = MeanReversionStrategy()

        result = strategy.analyze(
            symbol="OPUSDT",
            regime_result=self._range_result(),
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                {"open": 101.8, "high": 102.15, "low": 100.8, "close": 101.3, "volume": 720.0}
            ),
        )

        self.assertEqual(result["status"], STATUS_WATCH_ONLY)
        self.assertTrue(result["read_only"])
        self.assertTrue(result["execution_disabled"])
        self.assertEqual(result["side"], "SHORT")
        self.assertGreaterEqual(result["score"], result["threshold"])
        self.assertGreaterEqual(result["rr"], 1.2)

    def test_long_range_edge_returns_read_only_watch_candidate(self):
        strategy = MeanReversionStrategy()
        regime_result = self._range_result(
            metrics={
                "range_high": 102.0,
                "range_low": 98.0,
                "range_mid": 100.0,
                "range_position": 0.02,
            },
            setup={"status": "RANGE_EDGE_WATCH", "side": "LONG"},
        )

        result = strategy.analyze(
            symbol="ARBUSDT",
            regime_result=regime_result,
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                {"open": 98.2, "high": 99.3, "low": 97.85, "close": 98.7, "volume": 720.0}
            ),
        )

        self.assertEqual(result["status"], STATUS_WATCH_ONLY)
        self.assertEqual(result["side"], "LONG")
        self.assertGreaterEqual(result["rr"], 1.2)

    def test_waits_when_edge_reclaim_is_missing(self):
        strategy = MeanReversionStrategy()

        result = strategy.analyze(
            symbol="OPUSDT",
            regime_result=self._range_result(),
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                {"open": 101.2, "high": 101.3, "low": 100.8, "close": 101.25, "volume": 720.0}
            ),
        )

        self.assertEqual(result["status"], STATUS_WAIT_RECLAIM)
        self.assertIn("edge_touch", result["failed_checks"])

    def test_waits_when_volume_suggests_breakout_not_mean_reversion(self):
        strategy = MeanReversionStrategy()

        result = strategy.analyze(
            symbol="OPUSDT",
            regime_result=self._range_result(),
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                {"open": 101.8, "high": 102.15, "low": 100.8, "close": 101.3, "volume": 2200.0}
            ),
        )

        self.assertEqual(result["status"], STATUS_WAIT_VOLUME)
        self.assertIn("volume_exhaustion", result["failed_checks"])

    def test_rejects_when_risk_reward_is_too_small(self):
        strategy = MeanReversionStrategy()
        regime_result = self._range_result(
            metrics={
                "range_high": 102.0,
                "range_low": 101.0,
                "range_mid": 101.5,
                "range_position": 0.94,
            }
        )

        result = strategy.analyze(
            symbol="OPUSDT",
            regime_result=regime_result,
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                {"open": 101.8, "high": 102.1, "low": 101.55, "close": 101.7, "volume": 720.0}
            ),
        )

        self.assertEqual(result["status"], STATUS_REJECT_RR)
        self.assertIn("risk_reward", result["failed_checks"])

    def test_coordinator_selects_one_read_only_candidate(self):
        coordinator = ReadOnlyStrategyCoordinator()

        decision = coordinator.decide(
            symbol="OPUSDT",
            regime_result=self._range_result(),
            strategy_results=[
                {
                    "strategy": "MEAN_REVERSION",
                    "status": STATUS_WATCH_ONLY,
                    "side": "SHORT",
                    "score": 82,
                    "threshold": 70,
                    "rr": 1.8,
                    "order_type": "Limit",
                    "entry": 101.2,
                    "stop_loss": 102.2,
                    "target": 100.0,
                }
            ],
        )

        self.assertEqual(decision["decision"], DECISION_WATCH_ONLY)
        self.assertEqual(decision["selected_strategy"], "MEAN_REVERSION")
        self.assertTrue(decision["read_only"])
        self.assertTrue(decision["execution_disabled"])

    def test_coordinator_blocks_conflicting_sides_and_empty_candidates(self):
        coordinator = ReadOnlyStrategyCoordinator()

        no_action = coordinator.decide(
            symbol="OPUSDT",
            regime_result=self._range_result(),
            strategy_results=[],
        )
        conflict = coordinator.decide(
            symbol="OPUSDT",
            regime_result=self._range_result(),
            strategy_results=[
                {"strategy": "A", "status": STATUS_WATCH_ONLY, "side": "SHORT", "score": 80, "threshold": 70},
                {"strategy": "B", "status": STATUS_WATCH_ONLY, "side": "LONG", "score": 81, "threshold": 70},
            ],
        )

        self.assertEqual(no_action["decision"], DECISION_NO_ACTION)
        self.assertEqual(conflict["decision"], DECISION_CONFLICT)


if __name__ == "__main__":
    unittest.main()
