import os
import unittest

import pandas as pd

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from engine.market_regime import REGIME_RANGE, REGIME_TRENDING
from engine.strategies.trend_pullback import (
    STATUS_DISABLED,
    STATUS_WAIT_CONFIRMATION,
    STATUS_WAIT_PULLBACK,
    STATUS_WAIT_TREND,
    STATUS_WATCH_ONLY,
    TrendPullbackStrategy,
)


class TrendPullbackStrategyTests(unittest.TestCase):
    @staticmethod
    def _trend_result(**overrides):
        result = {
            "regime": REGIME_TRENDING,
            "confidence": 88,
            "trade_posture": "USE_SMC",
            "metrics": {
                "adx": 31.0,
                "efficiency_ratio": 0.42,
                "atr_percentile": 0.58,
            },
            "setup": {"status": "SMC_ONLY"},
        }
        result.update(overrides)
        return result

    @staticmethod
    def _hourly_trend(*, side: str) -> pd.DataFrame:
        rows = []
        for index in range(130):
            if side == "LONG":
                base = 100.0 + index * 0.22
                close = base + 0.08
            else:
                base = 130.0 - index * 0.22
                close = base - 0.08
            rows.append(
                {
                    "open": base,
                    "high": max(base, close) + 0.35,
                    "low": min(base, close) - 0.35,
                    "close": close,
                    "volume": 1200.0,
                }
            )
        rows.append({**rows[-1], "volume": 1.0})
        return pd.DataFrame(rows)

    @staticmethod
    def _hourly_sideways() -> pd.DataFrame:
        rows = []
        for index in range(130):
            base = 100.0 + (0.10 if index % 2 == 0 else -0.10)
            rows.append(
                {
                    "open": base,
                    "high": base + 0.35,
                    "low": base - 0.35,
                    "close": base,
                    "volume": 1200.0,
                }
            )
        rows.append({**rows[-1], "volume": 1.0})
        return pd.DataFrame(rows)

    @staticmethod
    def _fifteen_pullback(*, side: str, with_pullback: bool = True) -> pd.DataFrame:
        rows = []
        if side == "LONG":
            for index in range(82):
                base = 100.0 + index * 0.18
                rows.append(
                    {
                        "open": base,
                        "high": base + 0.32,
                        "low": base - 0.28,
                        "close": base + 0.08,
                        "volume": 1000.0,
                    }
                )
            if with_pullback:
                rows.extend(
                    [
                        {"open": 114.9, "high": 115.2, "low": 114.2, "close": 114.4, "volume": 820.0},
                        {"open": 114.4, "high": 115.3, "low": 114.3, "close": 115.0, "volume": 850.0},
                        {"open": 115.0, "high": 115.8, "low": 114.8, "close": 115.45, "volume": 900.0},
                    ]
                )
            else:
                rows.extend(
                    [
                        {"open": 116.8, "high": 117.4, "low": 116.6, "close": 117.2, "volume": 950.0},
                        {"open": 117.2, "high": 117.9, "low": 117.0, "close": 117.7, "volume": 950.0},
                        {"open": 117.7, "high": 118.4, "low": 117.5, "close": 118.2, "volume": 950.0},
                    ]
                )
        else:
            for index in range(82):
                base = 130.0 - index * 0.18
                rows.append(
                    {
                        "open": base,
                        "high": base + 0.28,
                        "low": base - 0.32,
                        "close": base - 0.08,
                        "volume": 1000.0,
                    }
                )
            rows.extend(
                [
                    {"open": 115.1, "high": 115.8, "low": 114.8, "close": 115.6, "volume": 820.0},
                    {"open": 115.6, "high": 115.7, "low": 114.7, "close": 115.0, "volume": 850.0},
                    {"open": 115.0, "high": 115.2, "low": 114.2, "close": 114.55, "volume": 900.0},
                ]
            )
        rows.append({**rows[-1], "volume": 1.0})
        return pd.DataFrame(rows)

    @staticmethod
    def _five_trigger(*, side: str, confirmed: bool = True) -> pd.DataFrame:
        rows = []
        if side == "LONG":
            for index in range(50):
                base = 114.8 + (0.03 if index % 2 == 0 else -0.03)
                rows.append(
                    {
                        "open": base,
                        "high": base + 0.14,
                        "low": base - 0.14,
                        "close": base,
                        "volume": 700.0,
                    }
                )
            trigger = (
                {"open": 114.9, "high": 115.75, "low": 114.8, "close": 115.55, "volume": 1050.0}
                if confirmed
                else {"open": 115.2, "high": 115.3, "low": 114.7, "close": 114.9, "volume": 600.0}
            )
        else:
            for index in range(50):
                base = 115.2 + (0.03 if index % 2 == 0 else -0.03)
                rows.append(
                    {
                        "open": base,
                        "high": base + 0.14,
                        "low": base - 0.14,
                        "close": base,
                        "volume": 700.0,
                    }
                )
            trigger = {"open": 115.1, "high": 115.2, "low": 114.25, "close": 114.45, "volume": 1050.0}
        rows.append(trigger)
        rows.append({**trigger, "volume": 1.0})
        return pd.DataFrame(rows)

    def test_disables_outside_trending_regime(self):
        strategy = TrendPullbackStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result={"regime": REGIME_RANGE, "setup": {"status": "RANGE_EDGE_WATCH"}},
            df_1h=self._hourly_trend(side="LONG"),
            df_15m=self._fifteen_pullback(side="LONG"),
            df_5m=self._five_trigger(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_DISABLED)
        self.assertEqual(result["failed_checks"], ["regime"])

    def test_waits_when_htf_trend_is_not_aligned(self):
        strategy = TrendPullbackStrategy()

        result = strategy.analyze(
            symbol="BTCUSDT",
            regime_result=self._trend_result(),
            df_1h=self._hourly_sideways(),
            df_15m=self._fifteen_pullback(side="LONG"),
            df_5m=self._five_trigger(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_WAIT_TREND)
        self.assertIn("htf_trend", result["failed_checks"])

    def test_long_pullback_returns_read_only_watch_candidate(self):
        strategy = TrendPullbackStrategy()

        result = strategy.analyze(
            symbol="SOLUSDT",
            regime_result=self._trend_result(),
            df_1h=self._hourly_trend(side="LONG"),
            df_15m=self._fifteen_pullback(side="LONG"),
            df_5m=self._five_trigger(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_WATCH_ONLY)
        self.assertEqual(result["side"], "LONG")
        self.assertTrue(result["read_only"])
        self.assertTrue(result["execution_disabled"])
        self.assertGreaterEqual(result["score"], result["threshold"])
        self.assertGreaterEqual(result["rr"], 1.4)

    def test_short_pullback_returns_read_only_watch_candidate(self):
        strategy = TrendPullbackStrategy()

        result = strategy.analyze(
            symbol="WIFUSDT",
            regime_result=self._trend_result(),
            df_1h=self._hourly_trend(side="SHORT"),
            df_15m=self._fifteen_pullback(side="SHORT"),
            df_5m=self._five_trigger(side="SHORT"),
        )

        self.assertEqual(result["status"], STATUS_WATCH_ONLY)
        self.assertEqual(result["side"], "SHORT")
        self.assertGreaterEqual(result["score"], result["threshold"])
        self.assertGreaterEqual(result["rr"], 1.4)

    def test_waits_when_price_is_chasing_without_pullback(self):
        strategy = TrendPullbackStrategy()

        result = strategy.analyze(
            symbol="SOLUSDT",
            regime_result=self._trend_result(),
            df_1h=self._hourly_trend(side="LONG"),
            df_15m=self._fifteen_pullback(side="LONG", with_pullback=False),
            df_5m=self._five_trigger(side="LONG"),
        )

        self.assertEqual(result["status"], STATUS_WAIT_PULLBACK)
        self.assertTrue(set(result["failed_checks"]) & {"value_touch", "not_chasing"})

    def test_waits_for_5m_confirmation(self):
        strategy = TrendPullbackStrategy()

        result = strategy.analyze(
            symbol="SOLUSDT",
            regime_result=self._trend_result(),
            df_1h=self._hourly_trend(side="LONG"),
            df_15m=self._fifteen_pullback(side="LONG"),
            df_5m=self._five_trigger(side="LONG", confirmed=False),
        )

        self.assertEqual(result["status"], STATUS_WAIT_CONFIRMATION)
        self.assertIn("trigger_direction", result["failed_checks"])


if __name__ == "__main__":
    unittest.main()
