import os
import unittest

import pandas as pd

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from engine.market_regime import REGIME_CHOP, REGIME_DATA_ERROR
from engine.strategies.volatility_expansion import (
    STATUS_DISABLED,
    STATUS_REJECT_EXTENSION,
    STATUS_WAIT_BREAKOUT,
    STATUS_WAIT_CONFIRMATION,
    STATUS_WAIT_EXPANSION,
    STATUS_WATCH_ONLY,
    VolatilityExpansionStrategy,
)


class VolatilityExpansionStrategyTests(unittest.TestCase):
    @staticmethod
    def _regime_result(**overrides):
        result = {
            "regime": REGIME_CHOP,
            "confidence": 82,
            "trade_posture": "NO_TRADE",
            "metrics": {
                "atr_percentile": 0.88,
                "relative_volume": 2.4,
                "efficiency_ratio": 0.30,
            },
            "setup": None,
        }
        result.update(overrides)
        return result

    @staticmethod
    def _hourly(*, side: str) -> pd.DataFrame:
        rows = []
        for index in range(95):
            if side == "LONG":
                base = 100.0 + index * 0.05
                close = base + 0.04
            elif side == "SHORT":
                base = 110.0 - index * 0.05
                close = base - 0.04
            else:
                base = 100.0 + (0.04 if index % 2 == 0 else -0.04)
                close = base
            rows.append(
                {
                    "open": base,
                    "high": max(base, close) + 0.25,
                    "low": min(base, close) - 0.25,
                    "close": close,
                    "volume": 1000.0,
                }
            )
        rows.append({**rows[-1], "volume": 1.0})
        return pd.DataFrame(rows)

    @staticmethod
    def _fifteen(*, side: str, close_inside: bool = False, overextended: bool = False, muted: bool = False) -> pd.DataFrame:
        rows = []
        for index in range(90):
            base = 100.0 + (0.04 if index % 2 == 0 else -0.04)
            rows.append(
                {
                    "open": base,
                    "high": base + 0.26,
                    "low": base - 0.26,
                    "close": base,
                    "volume": 800.0,
                }
            )

        if side == "LONG":
            trigger = {
                "open": 98.8 if close_inside else (92.0 if not overextended else 99.2),
                "high": 101.5 if close_inside else (101.0 if not overextended else 104.2),
                "low": 98.5 if close_inside else (91.8 if not overextended else 98.8),
                "close": 100.2 if close_inside else (100.68 if not overextended else 103.2),
                "volume": 900.0 if muted else 2600.0,
            }
        else:
            trigger = {
                "open": 108.0 if not overextended else 100.8,
                "high": 108.2 if not overextended else 101.2,
                "low": 98.6 if not overextended else 95.8,
                "close": 99.32 if not overextended else 96.8,
                "volume": 2600.0,
            }
        rows.append(trigger)
        rows.append({**trigger, "volume": 1.0})
        return pd.DataFrame(rows)

    @staticmethod
    def _five(*, side: str, confirmed: bool = True) -> pd.DataFrame:
        rows = []
        for index in range(48):
            base = 100.45 + (0.025 if index % 2 == 0 else -0.025)
            rows.append(
                {
                    "open": base,
                    "high": base + 0.08,
                    "low": base - 0.08,
                    "close": base,
                    "volume": 600.0,
                }
            )
        if side == "LONG":
            trigger = (
                {"open": 100.48, "high": 100.88, "low": 100.42, "close": 100.78, "volume": 1050.0}
                if confirmed
                else {"open": 100.72, "high": 100.8, "low": 100.32, "close": 100.36, "volume": 700.0}
            )
        else:
            trigger = {"open": 99.52, "high": 99.58, "low": 99.1, "close": 99.22, "volume": 1050.0}
        rows.append(trigger)
        rows.append({**trigger, "volume": 1.0})
        return pd.DataFrame(rows)

    def test_disables_data_error_regime(self):
        strategy = VolatilityExpansionStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._regime_result(regime=REGIME_DATA_ERROR),
            df_1h=self._hourly(side="LONG"),
            df_15m=self._fifteen(side="LONG"),
            df_5m=self._five(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_DISABLED)
        self.assertEqual(result["failed_checks"], ["regime"])

    def test_waits_when_impulse_volume_or_atr_expansion_is_missing(self):
        strategy = VolatilityExpansionStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._regime_result(),
            df_1h=self._hourly(side="LONG"),
            df_15m=self._fifteen(side="LONG", muted=True),
            df_5m=self._five(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_WAIT_EXPANSION)
        self.assertIn("volume_expansion", result["failed_checks"])

    def test_waits_for_local_range_break_after_expansion(self):
        strategy = VolatilityExpansionStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._regime_result(),
            df_1h=self._hourly(side="LONG"),
            df_15m=self._fifteen(side="LONG", close_inside=True),
            df_5m=self._five(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_WAIT_BREAKOUT)
        self.assertIn("range_break", result["failed_checks"])

    def test_rejects_already_overextended_expansion(self):
        strategy = VolatilityExpansionStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._regime_result(),
            df_1h=self._hourly(side="LONG"),
            df_15m=self._fifteen(side="LONG", overextended=True),
            df_5m=self._five(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_REJECT_EXTENSION)
        self.assertIn("extension", result["failed_checks"])

    def test_waits_for_5m_continuation_quality(self):
        strategy = VolatilityExpansionStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._regime_result(),
            df_1h=self._hourly(side="LONG"),
            df_15m=self._fifteen(side="LONG"),
            df_5m=self._five(side="LONG", confirmed=False),
        )

        self.assertEqual(result["status"], STATUS_WAIT_CONFIRMATION)
        self.assertIn("continuation_direction", result["failed_checks"])

    def test_long_expansion_returns_read_only_watch_candidate(self):
        strategy = VolatilityExpansionStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._regime_result(),
            df_1h=self._hourly(side="LONG"),
            df_15m=self._fifteen(side="LONG"),
            df_5m=self._five(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_WATCH_ONLY)
        self.assertEqual(result["side"], "LONG")
        self.assertTrue(result["read_only"])
        self.assertTrue(result["execution_disabled"])
        self.assertGreaterEqual(result["score"], result["threshold"])
        self.assertGreaterEqual(result["rr"], 1.5)

    def test_short_expansion_returns_read_only_watch_candidate(self):
        strategy = VolatilityExpansionStrategy()

        result = strategy.analyze(
            symbol="ETHUSDT",
            regime_result=self._regime_result(),
            df_1h=self._hourly(side="SHORT"),
            df_15m=self._fifteen(side="SHORT"),
            df_5m=self._five(side="SHORT"),
        )

        self.assertEqual(result["status"], STATUS_WATCH_ONLY)
        self.assertEqual(result["side"], "SHORT")
        self.assertGreaterEqual(result["score"], result["threshold"])
        self.assertGreaterEqual(result["rr"], 1.5)


if __name__ == "__main__":
    unittest.main()
