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

    def test_exchange_sync_matches_positions_by_symbol_and_side(self):
        self._install_fake_exchange_deps()

        from core.exchange import ExchangeManager

        class FakeDB:
            def __init__(self):
                self.closed = []
                self.opened = []

            def mark_trade_open(self, **kwargs):
                self.opened.append(kwargs)
                return False

            def get_open_positions(self):
                return [{"id": 7, "symbol": "BTCUSDT", "side": "LONG"}]

            def get_pending_orders(self):
                return []

            def close_trade(self, **kwargs):
                self.closed.append(kwargs)
                return True

        manager = ExchangeManager.__new__(ExchangeManager)
        manager.retry_attempts = 1
        manager.logger = logging.getLogger("test.ExchangeManager")
        manager.session = types.SimpleNamespace()
        manager.get_latest_closed_pnl = lambda symbol: None
        manager.get_active_positions = lambda: [
            {
                "symbol": "BTCUSDT",
                "side": "Sell",
                "size": 1.0,
                "entry_price": 100.0,
                "stop_loss": 105.0,
            }
        ]

        db = FakeDB()
        manager.sync_db_with_exchange(db)

        self.assertEqual(len(db.closed), 1)
        self.assertEqual(db.closed[0]["symbol"], "BTCUSDT")
        self.assertEqual(db.closed[0]["status"], "CLOSED_UNVERIFIED")

    def test_unverified_close_is_excluded_from_pnl_stats(self):
        from core.database import TradeDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TradeDatabase(os.path.join(tmpdir, "bot_memory.db"))

            unverified_id = db.add_trade(
                {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100.0,
                    "qty": 1.0,
                    "sl": 95.0,
                    "score": 80,
                    "status": "OPEN",
                }
            )
            verified_id = db.add_trade(
                {
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "entry": 100.0,
                    "qty": 1.0,
                    "sl": 105.0,
                    "score": 80,
                    "status": "OPEN",
                }
            )

            self.assertTrue(
                db.close_trade("BTCUSDT", 0.0, -50.0, 0.0, trade_id=unverified_id, status="CLOSED_UNVERIFIED")
            )
            self.assertTrue(
                db.close_trade("ETHUSDT", 90.0, 20.0, 2.0, trade_id=verified_id, status="CLOSED")
            )

            self.assertEqual(db.get_today_pnl_usd(), 20.0)
            stats = db.get_stats()
            self.assertEqual(stats["total_trades"], 1)
            self.assertEqual(stats["net_pnl"], 20.0)

            db.close()

    def test_risk_manager_counts_pending_orders_toward_trade_limit(self):
        from core.risk_manager import RiskManager

        class FakeExchange:
            def get_pending_entry_orders(self, symbol=None):
                return [
                    {
                        "symbol": symbol or "BTCUSDT",
                        "orderType": "Limit",
                        "orderStatus": "New",
                        "reduceOnly": False,
                        "closeOnTrigger": False,
                    }
                ]

        manager = RiskManager(balance=1000.0)
        manager._get_risk_settings = lambda: {
            "max_daily_loss_pct": 50.0,
            "max_open_trades": 1,
            "risk_per_trade_pct": 1.0,
        }

        allowed, reason = manager.check_safety_filters(
            daily_pnl_usd=0.0,
            active_positions=[],
            symbol="ETHUSDT",
            exchange_manager=FakeExchange(),
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "MAX_TRADES_LIMIT")

    def test_risk_manager_fails_closed_when_pending_check_fails(self):
        from core.risk_manager import RiskManager

        class FailingExchange:
            def get_pending_entry_orders(self, symbol=None):
                raise RuntimeError("api down")

        manager = RiskManager(balance=1000.0)
        manager._get_risk_settings = lambda: {
            "max_daily_loss_pct": 50.0,
            "max_open_trades": 5,
            "risk_per_trade_pct": 1.0,
        }

        allowed, reason = manager.check_safety_filters(
            daily_pnl_usd=0.0,
            active_positions=[],
            symbol="ETHUSDT",
            exchange_manager=FailingExchange(),
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "PENDING_ORDER_CHECK_FAILED")

    def test_position_manager_restores_open_trade_before_tp_cascade(self):
        from core.position_manager import PositionManager

        class FakeDatabaseSync:
            def get_open_trade(self, symbol, side=None):
                return {
                    "symbol": symbol,
                    "side": side,
                    "qty": 1.0,
                    "entry_price": 100.0,
                    "stop_loss": 95.0,
                }

        class FakeTPManager:
            def __init__(self):
                self.placed = []

            def calculate_tp_levels(self, entry, stop, side):
                return {"tp1": 105.0, "tp2": 115.0}

            def normalize_tp_levels(self, symbol, tp_levels, side):
                return dict(tp_levels)

            def place_cascade_tps(self, **kwargs):
                self.placed.append(kwargs)
                return True

        manager = PositionManager.__new__(PositionManager)
        manager.position_cache = {}
        manager.database_sync = FakeDatabaseSync()
        manager.tp_manager = FakeTPManager()
        manager.logger = logging.getLogger("test.PositionManager")

        manager._check_and_place_missing_tps(
            symbol="BTCUSDT",
            side="LONG",
            current_size=0.95,
            entry_price=100.0,
            current_sl=95.0,
            position_idx=1,
        )

        self.assertTrue(manager.position_cache["BTCUSDT"]["tps_placed"])
        self.assertEqual(manager.tp_manager.placed[0]["total_qty"], 1.0)

    def test_executor_order_params_include_order_link_id_and_no_none_values(self):
        from core.executor import TradeExecutor

        class FakeSession:
            def __init__(self):
                self.order_params = None

            def get_tickers(self, **kwargs):
                return {"retCode": 0, "result": {"list": [{"lastPrice": "100"}]}}

            def place_order(self, **kwargs):
                self.order_params = kwargs
                return {"retCode": 0, "result": {"orderId": "order-1"}}

        class FakeInstruments:
            def refresh(self, symbol):
                return True

            def normalize_qty(self, symbol, qty):
                return qty

            def validate_order_size(self, symbol, qty, price):
                return True

            def normalize_stop(self, symbol, sl, side):
                return sl

        class FakeTPManager:
            def calculate_tp_levels(self, entry, stop, side):
                return {"tp1": 105.0}

            def normalize_tp_levels(self, symbol, tp_levels, side):
                return dict(tp_levels)

            def validate_tp_levels(self, symbol, entry, side, tp_levels):
                return True

            def place_cascade_tps(self, **kwargs):
                return True

        class FakePositionManager:
            def __init__(self):
                self.positions = []

            def remember_position(self, **kwargs):
                self.positions.append(kwargs)

        class FakeDatabaseSync:
            def __init__(self):
                self.saved = []

            def save_open_trade(self, **kwargs):
                self.saved.append(kwargs)
                return True

        class FakeAudit:
            def log_trade_event(self, *args, **kwargs):
                return None

        session = FakeSession()
        executor = TradeExecutor.__new__(TradeExecutor)
        executor.session = session
        executor.logger = logging.getLogger("test.TradeExecutor")
        executor.audit = FakeAudit()
        executor.instruments = FakeInstruments()
        executor.tp_manager = FakeTPManager()
        executor.position_manager = FakePositionManager()
        executor.database_sync = FakeDatabaseSync()
        executor.retry_attempts = 1
        executor.max_slippage_pct = 1.0

        result = executor.execute_institutional_entry(
            symbol="BTCUSDT",
            side="LONG",
            poi={"side": "LONG", "mid": 100.0, "top": 101.0, "bottom": 99.0},
            score=90,
            qty=1.0,
            sl=95.0,
            risk_pct=1.0,
            order_type="Limit",
            limit_price=99.5,
        )

        self.assertIsNotNone(result)
        self.assertIn("orderLinkId", session.order_params)
        self.assertTrue(all(value is not None for value in session.order_params.values()))
        self.assertEqual(session.order_params["timeInForce"], "GTC")
        self.assertEqual(executor.database_sync.saved[0]["status"], "PENDING_ORDER")


if __name__ == "__main__":
    unittest.main()
