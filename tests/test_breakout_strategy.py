import os
import unittest

import pandas as pd

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from engine.market_regime import REGIME_LOW_VOL_COMPRESSION, REGIME_RANGE
from engine.strategies.breakout import (
    STATUS_DISABLED,
    STATUS_WAIT_BREAKOUT,
    STATUS_WAIT_RETEST,
    STATUS_WAIT_VOLUME,
    STATUS_WATCH_ONLY,
    BreakoutStrategy,
)


class BreakoutStrategyTests(unittest.TestCase):
    @staticmethod
    def _compression_result(**overrides):
        result = {
            "regime": REGIME_LOW_VOL_COMPRESSION,
            "confidence": 88,
            "trade_posture": "WAIT_BREAKOUT",
            "metrics": {
                "range_high": 102.0,
                "range_low": 98.0,
                "range_mid": 100.0,
            },
            "setup": {
                "status": "WAIT_BREAKOUT",
                "breakout_above": 102.0,
                "breakout_below": 98.0,
            },
        }
        result.update(overrides)
        return result

    @staticmethod
    def _fifteen_min_data() -> pd.DataFrame:
        rows = []
        for index in range(45):
            base = 100.0 + (0.05 if index % 2 == 0 else -0.05)
            rows.append(
                {
                    "open": base,
                    "high": base + 0.35,
                    "low": base - 0.35,
                    "close": base + 0.01,
                    "volume": 1000.0,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _five_min_data(extra_rows: list[dict[str, float]]) -> pd.DataFrame:
        rows = []
        for index in range(50):
            base = 100.0 + (0.04 if index % 2 == 0 else -0.04)
            rows.append(
                {
                    "open": base,
                    "high": base + 0.35,
                    "low": base - 0.35,
                    "close": base,
                    "volume": 500.0,
                }
            )
        rows.extend(extra_rows)
        rows.append(
            {
                "open": rows[-1]["close"],
                "high": rows[-1]["close"] + 0.01,
                "low": rows[-1]["close"] - 0.01,
                "close": rows[-1]["close"],
                "volume": 1.0,
            }
        )
        return pd.DataFrame(rows)

    def test_disables_outside_compression_regime(self):
        strategy = BreakoutStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result={"regime": REGIME_RANGE, "setup": {"status": "RANGE_EDGE_WATCH"}},
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data([]),
        )

        self.assertEqual(result["status"], STATUS_DISABLED)
        self.assertEqual(result["failed_checks"], ["regime"])

    def test_waits_before_price_closes_outside_compression_range(self):
        strategy = BreakoutStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._compression_result(),
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                [{"open": 100.0, "high": 101.8, "low": 99.2, "close": 101.6, "volume": 900.0}]
            ),
        )

        self.assertEqual(result["status"], STATUS_WAIT_BREAKOUT)
        self.assertIn("range_break", result["failed_checks"])

    def test_waits_for_volume_expansion_after_breakout_close(self):
        strategy = BreakoutStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._compression_result(),
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                [{"open": 101.6, "high": 103.0, "low": 101.4, "close": 102.8, "volume": 550.0}]
            ),
        )

        self.assertEqual(result["status"], STATUS_WAIT_VOLUME)
        self.assertIn("volume_expansion", result["failed_checks"])

    def test_fresh_breakout_waits_for_retest_before_watch(self):
        strategy = BreakoutStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._compression_result(),
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                [{"open": 101.4, "high": 103.0, "low": 101.3, "close": 102.7, "volume": 1100.0}]
            ),
        )

        self.assertEqual(result["status"], STATUS_WAIT_RETEST)
        self.assertIn("retest", result["failed_checks"])

    def test_long_breakout_retest_returns_read_only_watch_candidate(self):
        strategy = BreakoutStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._compression_result(),
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                [
                    {"open": 101.4, "high": 103.0, "low": 101.3, "close": 102.7, "volume": 1500.0},
                    {"open": 102.7, "high": 102.8, "low": 101.9, "close": 102.25, "volume": 680.0},
                    {"open": 102.25, "high": 102.95, "low": 102.1, "close": 102.65, "volume": 720.0},
                ]
            ),
        )

        self.assertEqual(result["status"], STATUS_WATCH_ONLY)
        self.assertEqual(result["side"], "LONG")
        self.assertTrue(result["read_only"])
        self.assertTrue(result["execution_disabled"])
        self.assertGreaterEqual(result["score"], result["threshold"])
        self.assertGreaterEqual(result["rr"], 1.4)

    def test_short_breakout_retest_returns_read_only_watch_candidate(self):
        strategy = BreakoutStrategy()

        result = strategy.analyze(
            symbol="ETHUSDT",
            regime_result=self._compression_result(),
            df_15m=self._fifteen_min_data(),
            df_5m=self._five_min_data(
                [
                    {"open": 98.6, "high": 98.8, "low": 97.0, "close": 97.3, "volume": 1500.0},
                    {"open": 97.3, "high": 98.1, "low": 97.2, "close": 97.75, "volume": 680.0},
                    {"open": 97.75, "high": 97.9, "low": 97.05, "close": 97.35, "volume": 720.0},
                ]
            ),
        )

        self.assertEqual(result["status"], STATUS_WATCH_ONLY)
        self.assertEqual(result["side"], "SHORT")
        self.assertGreaterEqual(result["score"], result["threshold"])
        self.assertGreaterEqual(result["rr"], 1.4)


if __name__ == "__main__":
    unittest.main()
