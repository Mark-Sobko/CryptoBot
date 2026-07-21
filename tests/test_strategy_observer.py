import os
import unittest

import pandas as pd

os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from scripts.run_strategy_observer import (
    news_score_bonus,
    parse_symbols,
    validate_market_data,
    validate_read_only_environment,
)


class StrategyObserverTests(unittest.TestCase):
    def test_read_only_environment_blocks_production_by_default(self):
        with self.assertRaises(RuntimeError):
            validate_read_only_environment(
                demo=False,
                testnet=False,
                allow_production_read_only=False,
            )

        validate_read_only_environment(
            demo=False,
            testnet=False,
            allow_production_read_only=True,
        )
        validate_read_only_environment(
            demo=True,
            testnet=False,
            allow_production_read_only=False,
        )

    def test_parse_symbols_normalizes_deduplicates_and_limits(self):
        symbols = parse_symbols(" btcusdt,ETHUSDT, btcusdt, solusdt ", ["XRPUSDT"], 2)

        self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT"])

    def test_news_score_bonus_matches_main_direction_logic(self):
        self.assertEqual(news_score_bonus("LONG", "LONG"), 15)
        self.assertEqual(news_score_bonus("SHORT", "LONG"), -20)
        self.assertEqual(news_score_bonus("NONE", "LONG"), 0)
        self.assertEqual(news_score_bonus("BLOCK", "LONG"), -100)

    def test_validate_market_data_accepts_required_timeframes(self):
        frame = pd.DataFrame(
            {
                "open": [1.0] * 100,
                "high": [1.1] * 100,
                "low": [0.9] * 100,
                "close": [1.0] * 100,
                "volume": [10.0] * 100,
            }
        )

        ok, reason = validate_market_data(
            {
                "5m": frame,
                "15m": frame,
                "1h": frame,
                "4h": frame,
            }
        )

        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_validate_market_data_reports_missing_timeframe(self):
        ok, reason = validate_market_data({})

        self.assertFalse(ok)
        self.assertEqual(reason, "missing_timeframe:5m")


if __name__ == "__main__":
    unittest.main()
