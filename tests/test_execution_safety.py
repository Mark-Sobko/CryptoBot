import os
import logging
import sys
import tempfile
import types
import unittest


os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")


class ExecutionSafetyTests(unittest.TestCase):
    @staticmethod
    def _install_fake_exchange_deps():
        if "pandas" not in sys.modules:
            pandas = types.ModuleType("pandas")

            class DataFrame:
                pass

            pandas.DataFrame = DataFrame
            sys.modules["pandas"] = pandas

        if "pybit.unified_trading" in sys.modules:
            return

        if "requests" not in sys.modules:
            requests = types.ModuleType("requests")

            class Timeout(Exception):
                pass

            requests.exceptions = types.SimpleNamespace(Timeout=Timeout)
            sys.modules["requests"] = requests

        pybit = types.ModuleType("pybit")
        unified_trading = types.ModuleType("pybit.unified_trading")

        class HTTP:
            pass

        unified_trading.HTTP = HTTP
        sys.modules["pybit"] = pybit
        sys.modules["pybit.unified_trading"] = unified_trading

    def test_poi_reference_price_falls_back_to_midpoint(self):
        from core.executor import TradeExecutor

        self.assertEqual(
            TradeExecutor._get_poi_reference_price({"top": 110.0, "bottom": 100.0}),
            105.0,
        )
        self.assertEqual(
            TradeExecutor._get_poi_reference_price({"mid": 103.5, "top": 110.0, "bottom": 100.0}),
            103.5,
        )

    def test_database_keeps_pending_limit_out_of_open_positions(self):
        from core.database import TradeDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TradeDatabase(os.path.join(tmpdir, "bot_memory.db"))

            trade_id = db.add_trade(
                {
                    "order_id": "limit-1",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100.0,
                    "qty": 0.1,
                    "sl": 95.0,
                    "score": 80,
                    "status": "PENDING_ORDER",
                }
            )

            self.assertIsNotNone(trade_id)
            self.assertEqual(db.get_open_positions(), [])
            self.assertEqual(len(db.get_pending_orders()), 1)

            self.assertTrue(
                db.mark_trade_open(
                    symbol="BTCUSDT",
                    side="LONG",
                    entry_price=101.0,
                    qty=0.1,
                    stop_loss=96.0,
                )
            )

            open_positions = db.get_open_positions()
            self.assertEqual(len(open_positions), 1)
            self.assertEqual(open_positions[0]["status"], "OPEN")
            self.assertEqual(open_positions[0]["entry_price"], 101.0)

            db.close()

    def test_tp_cleanup_only_cancels_reduce_only_orders(self):
        from core.tp_manager import TPManager

        class FakeSession:
            def __init__(self):
                self.cancelled = []

            def set_trading_stop(self, **kwargs):
                return {"retCode": 0, "result": {}}

            def get_open_orders(self, **kwargs):
                return {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {"orderId": "tp-1", "reduceOnly": True},
                            {"orderId": "manual-1", "reduceOnly": False},
                        ]
                    },
                }

            def cancel_order(self, **kwargs):
                self.cancelled.append(kwargs["orderId"])
                return {"retCode": 0, "result": {}}

        session = FakeSession()
        manager = TPManager(session=session, instruments=None)
        manager.request_delay = 0
        manager._cancel_existing_tps("BTCUSDT", position_idx=1)

        self.assertEqual(session.cancelled, ["tp-1"])

    def test_risk_manager_blocks_new_entries_when_position_has_no_stop(self):
        from core.risk_manager import RiskManager

        manager = RiskManager(balance=1000.0)
        allowed, reason = manager.check_safety_filters(
            daily_pnl_usd=0.0,
            active_positions=[
                {
                    "symbol": "BTCUSDT",
                    "entry_price": 100.0,
                    "stop_loss": 0.0,
                    "size": 1.0,
                }
            ],
            symbol="ETHUSDT",
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "POSITION_WITHOUT_STOP")

    def test_exchange_sync_preserves_live_pending_order_and_cancels_stale_one(self):
        self._install_fake_exchange_deps()

        from core.exchange import ExchangeManager

        class FakeSession:
            def __init__(self, open_orders):
                self.open_orders = open_orders

            def get_open_orders(self, **kwargs):
                return {"retCode": 0, "result": {"list": self.open_orders}}

        class FakeDB:
            def __init__(self):
                self.opened = []
                self.closed = []
                self.cancelled = []
                self.pending = []

            def mark_trade_open(self, **kwargs):
                self.opened.append(kwargs)
                return True

            def get_open_positions(self):
                return []

            def get_pending_orders(self):
                return list(self.pending)

            def close_trade(self, **kwargs):
                self.closed.append(kwargs)
                return True

            def mark_trade_cancelled(self, **kwargs):
                self.cancelled.append(kwargs)
                return True

        manager = ExchangeManager.__new__(ExchangeManager)
        manager.retry_attempts = 1
        manager.logger = logging.getLogger("test.ExchangeManager")
        manager.get_active_positions = lambda: []

        db = FakeDB()
        db.pending = [{"id": 1, "symbol": "BTCUSDT", "order_id": "limit-1"}]
        manager.session = FakeSession(open_orders=[{"orderId": "limit-1"}])
        manager.sync_db_with_exchange(db)
        self.assertEqual(db.cancelled, [])
        self.assertEqual(db.closed, [])

        manager.session = FakeSession(open_orders=[])
        manager.sync_db_with_exchange(db)
        self.assertEqual(len(db.cancelled), 1)
        self.assertEqual(db.cancelled[0]["order_id"], "limit-1")


if __name__ == "__main__":
    unittest.main()
