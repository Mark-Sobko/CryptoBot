import os
import datetime
import importlib
import logging
import sys
import tempfile
import types
import unittest
from unittest import mock


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

    @staticmethod
    def _import_institutional_bot():
        pandas_module = sys.modules.get("pandas")
        if pandas_module is not None and getattr(pandas_module, "__file__", None) is None:
            sys.modules.pop("pandas", None)

        for module_name in list(sys.modules):
            if module_name in {"main", "engine.filters"} or module_name.startswith("pandas_ta"):
                sys.modules.pop(module_name, None)

        return importlib.import_module("main").InstitutionalBot

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

    def test_database_persists_strategy_metadata(self):
        from core.database import TradeDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TradeDatabase(os.path.join(tmpdir, "bot_memory.db"))
            db.add_trade(
                {
                    "symbol": "WIFUSDT",
                    "side": "SHORT",
                    "entry": 0.154,
                    "qty": 10.0,
                    "sl": 0.156,
                    "score": 82,
                    "poi_type": "ALT_MEAN_REVERSION",
                    "strategy": "MEAN_REVERSION",
                    "source": "ALT_STRATEGY",
                    "max_hold_minutes": 120.0,
                    "status": "PENDING_ORDER",
                }
            )

            trade = db.get_last_trade("WIFUSDT")
            self.assertEqual(trade["strategy"], "MEAN_REVERSION")
            self.assertEqual(trade["source"], "ALT_STRATEGY")
            self.assertEqual(trade["max_hold_minutes"], 120.0)
            db.close()

    def test_database_reconciles_open_trade_qty_after_partial_fill_grows(self):
        from core.database import TradeDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TradeDatabase(os.path.join(tmpdir, "bot_memory.db"))

            db.add_trade(
                {
                    "order_id": "limit-1",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "entry": 100.0,
                    "qty": 1.0,
                    "sl": 95.0,
                    "score": 80,
                    "status": "PENDING_ORDER",
                }
            )

            self.assertTrue(
                db.mark_trade_open(
                    symbol="BTCUSDT",
                    side="LONG",
                    entry_price=100.0,
                    qty=0.4,
                    stop_loss=95.0,
                )
            )
            self.assertTrue(
                db.mark_trade_open(
                    symbol="BTCUSDT",
                    side="LONG",
                    entry_price=100.0,
                    qty=1.0,
                    stop_loss=95.0,
                )
            )

            open_positions = db.get_open_positions()
            self.assertEqual(len(open_positions), 1)
            self.assertEqual(open_positions[0]["qty"], 1.0)

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

    def test_tp_cleanup_treats_bybit_not_modified_clear_as_noop(self):
        from core.tp_manager import TPManager

        class FakeSession:
            def __init__(self):
                self.clear_attempts = 0
                self.open_orders_called = False

            def set_trading_stop(self, **kwargs):
                self.clear_attempts += 1
                raise RuntimeError("not modified (ErrCode: 34040)")

            def get_open_orders(self, **kwargs):
                self.open_orders_called = True
                return {"retCode": 0, "result": {"list": []}}

        session = FakeSession()
        manager = TPManager(session=session, instruments=None)
        manager.request_delay = 0
        manager._cancel_existing_tps("ETHUSDT", position_idx=2)

        self.assertEqual(session.clear_attempts, 1)
        self.assertTrue(session.open_orders_called)

    def test_tp_split_preserves_normalized_total_qty(self):
        from core.tp_manager import TPManager

        class FakeInstruments:
            def normalize_qty(self, symbol, qty):
                return int(float(qty) * 1000) / 1000

        manager = TPManager(session=types.SimpleNamespace(), instruments=FakeInstruments())

        parts = manager._split_qty("BTCUSDT", 0.4, 3)

        self.assertEqual(parts, [0.2, 0.1, 0.1])
        self.assertEqual(round(sum(parts), 8), 0.4)

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

    def test_exchange_sync_uses_mark_price_when_entry_price_is_missing(self):
        self._install_fake_exchange_deps()

        from core.exchange import ExchangeManager

        class FakeDB:
            def __init__(self):
                self.opened = []

            def mark_trade_open(self, **kwargs):
                self.opened.append(kwargs)
                return True

            def get_open_positions(self):
                return []

            def get_pending_orders(self):
                return []

        manager = ExchangeManager.__new__(ExchangeManager)
        manager.retry_attempts = 1
        manager.logger = logging.getLogger("test.ExchangeManager")
        manager.session = types.SimpleNamespace()
        manager.get_active_positions = lambda: [
            {
                "symbol": "INJUSDT",
                "side": "Sell",
                "size": 109.3,
                "entry_price": 0.0,
                "mark_price": 4.6285,
                "stop_loss": 4.72,
            }
        ]

        db = FakeDB()
        manager.sync_db_with_exchange(db)

        self.assertEqual(db.opened[0]["entry_price"], 4.6285)
        self.assertEqual(db.opened[0]["side"], "SHORT")

    def test_database_adopts_exchange_visible_open_position(self):
        from core.database import TradeDatabase

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TradeDatabase(os.path.join(tmpdir, "bot_memory.db"))

            adopted = db.mark_trade_open(
                symbol="INJUSDT",
                side="SHORT",
                entry_price=4.6285,
                qty=109.3,
                stop_loss=4.72,
            )
            trade = db.get_open_trade("INJUSDT", "SHORT")

            self.assertTrue(adopted)
            self.assertIsNotNone(trade)
            self.assertEqual(trade["strategy"], "EXCHANGE_SYNC")
            self.assertEqual(trade["source"], "EXCHANGE_SYNC")
            self.assertEqual(trade["status"], "OPEN")
            self.assertAlmostEqual(trade["entry_price"], 4.6285)
            db.close()

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
        self.assertEqual(manager.tp_manager.placed[0]["total_qty"], 0.95)

    def test_position_manager_uses_mark_price_for_missing_entry_emergency_stop(self):
        from core.position_manager import PositionManager

        class FakeInstruments:
            def get(self, symbol):
                return True

        manager = PositionManager.__new__(PositionManager)
        manager.logger = logging.getLogger("test.PositionManager")
        manager.instruments = FakeInstruments()
        manager.set_emergency_stop = mock.Mock()

        manager.manage_position(
            {
                "symbol": "ETHUSDT",
                "side": "Sell",
                "size": 0.58,
                "entryPrice": 0.0,
                "markPrice": 1919.43,
                "stopLoss": 0.0,
                "positionIdx": 2,
            }
        )

        manager.set_emergency_stop.assert_called_once_with("ETHUSDT", "SHORT", 1919.43, 2)

    def test_position_manager_places_tps_for_partial_fill_visible_qty(self):
        from core.position_manager import PositionManager

        class FakeDatabaseSync:
            def get_open_trade(self, symbol, side=None):
                return None

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
        manager.position_cache = {
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "initial_qty": 1.0,
                "entry_price": 100.0,
                "sl": 95.0,
                "position_idx": 1,
                "tps_placed": False,
                "tp_qty": 0.0,
            }
        }
        manager.database_sync = FakeDatabaseSync()
        manager.tp_manager = FakeTPManager()
        manager.logger = logging.getLogger("test.PositionManager")

        manager._check_and_place_missing_tps(
            symbol="BTCUSDT",
            side="LONG",
            current_size=0.4,
            entry_price=100.0,
            current_sl=95.0,
            position_idx=1,
        )

        self.assertTrue(manager.position_cache["BTCUSDT"]["tps_placed"])
        self.assertEqual(manager.position_cache["BTCUSDT"]["tp_qty"], 0.4)
        self.assertEqual(manager.tp_manager.placed[0]["total_qty"], 0.4)

    def test_position_manager_refreshes_tps_when_partial_fill_grows(self):
        from core.position_manager import PositionManager

        class FakeDatabaseSync:
            def get_open_trade(self, symbol, side=None):
                return None

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
        manager.position_cache = {
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "initial_qty": 1.0,
                "entry_price": 100.0,
                "sl": 95.0,
                "position_idx": 1,
                "tps_placed": True,
                "tp_qty": 0.4,
            }
        }
        manager.database_sync = FakeDatabaseSync()
        manager.tp_manager = FakeTPManager()
        manager.logger = logging.getLogger("test.PositionManager")

        manager._check_and_place_missing_tps(
            symbol="BTCUSDT",
            side="LONG",
            current_size=0.8,
            entry_price=100.0,
            current_sl=95.0,
            position_idx=1,
        )

        self.assertEqual(manager.position_cache["BTCUSDT"]["tp_qty"], 0.8)
        self.assertEqual(manager.tp_manager.placed[0]["total_qty"], 0.8)


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
        self.assertEqual(executor.database_sync.saved[0]["strategy"], "SMC")
        self.assertEqual(executor.database_sync.saved[0]["source"], "SMC")
        self.assertEqual(executor.database_sync.saved[0]["max_hold_minutes"], 0.0)

    def test_executor_limit_order_uses_strategy_tp_override(self):
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

            def normalize_tp(self, symbol, tp, side):
                return tp

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
            poi={"type": "ALT_MEAN_REVERSION", "side": "LONG", "mid": 100.0},
            score=85,
            qty=1.0,
            sl=95.0,
            risk_pct=1.0,
            order_type="Limit",
            limit_price=100.0,
            tp_levels_override={"tp1": 112.0},
        )

        self.assertIsNotNone(result)
        self.assertEqual(session.order_params["takeProfit"], "112.0")
        self.assertTrue(executor.position_manager.positions[0]["tps_placed"])
        self.assertEqual(executor.database_sync.saved[0]["poi_type"], "ALT_MEAN_REVERSION")
        self.assertAlmostEqual(executor.database_sync.saved[0]["rr"], 2.4)

    def test_main_refuses_live_mode_without_explicit_config_flag(self):
        import config
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        global_cfg = dict(config.RISK_MANAGEMENT["global"])
        global_cfg["allow_live_trading"] = False
        risk_cfg = dict(config.RISK_MANAGEMENT)
        risk_cfg["global"] = global_cfg

        with (
            mock.patch.object(config, "BYBIT_DEMO", False),
            mock.patch.object(config, "BYBIT_TESTNET", False),
            mock.patch.object(config, "RISK_MANAGEMENT", risk_cfg),
            self.assertRaisesRegex(RuntimeError, "refuses live trading"),
        ):
            bot._validate_runtime_environment()

    def test_main_allows_demo_runtime_environment(self):
        import config
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)

        with (
            mock.patch.object(config, "BYBIT_DEMO", True),
            mock.patch.object(config, "BYBIT_TESTNET", False),
        ):
            bot._validate_runtime_environment()

    def test_main_runtime_limit_stops_gracefully(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.is_running = True
        bot.max_runtime_minutes = 1.0
        bot.started_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=2)

        self.assertTrue(bot._runtime_limit_reached())
        self.assertFalse(bot.is_running)

    def test_main_drawdown_limit_stops_gracefully(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.is_running = True
        bot.initial_balance = 1000.0
        bot.max_drawdown_limit_pct = 10.0
        bot.last_drawdown_pct = 0.0

        self.assertTrue(bot._drawdown_limit_reached(890.0))
        self.assertFalse(bot.is_running)
        self.assertAlmostEqual(bot.last_drawdown_pct, 11.0)

    def test_main_caps_qty_by_order_notional(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.max_order_notional_usd = 25.0

        self.assertAlmostEqual(bot._cap_qty_by_notional("WIFUSDT", 1000.0, 0.05), 500.0)
        self.assertEqual(bot._cap_qty_by_notional("WIFUSDT", 10.0, 0.05), 10.0)

    def test_main_order_guards_block_run_and_cycle_overflow(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.execution_enabled = True
        bot.max_orders_per_run = 1
        bot.max_orders_per_cycle = 1
        bot.orders_submitted_this_run = 1
        bot.orders_submitted_this_cycle = 0

        self.assertFalse(bot._execution_guard_allows_new_order("BTCUSDT"))

        bot.orders_submitted_this_run = 0
        bot.orders_submitted_this_cycle = 1

        self.assertFalse(bot._execution_guard_allows_new_order("ETHUSDT"))

        bot.orders_submitted_this_cycle = 0

        self.assertTrue(bot._execution_guard_allows_new_order("SOLUSDT"))

    def test_main_execution_enabled_guard_blocks_new_orders(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.execution_enabled = False
        bot.max_orders_per_run = 0
        bot.max_orders_per_cycle = 0
        bot.orders_submitted_this_run = 0
        bot.orders_submitted_this_cycle = 0

        self.assertFalse(bot._execution_guard_allows_new_order("BTCUSDT"))

    @staticmethod
    def _alt_strategy_result():
        return {
            "decision": {
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
            },
        }

    def test_main_multi_strategy_execution_flag_defaults_to_no_order(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.multi_strategy_execution_enabled = False
        bot.executor = mock.Mock()

        result = bot._maybe_execute_read_only_strategy(
            "WIFUSDT",
            self._alt_strategy_result(),
            {"max_open_trades": 1, "risk_per_trade_pct": 1.0},
        )

        self.assertIsNone(result)
        bot.executor.execute_institutional_entry.assert_not_called()

    def test_main_multi_strategy_execution_routes_valid_plan_through_executor(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.multi_strategy_execution_enabled = True
        bot.multi_strategy_allowed_strategies = {"MEAN_REVERSION"}
        bot.multi_strategy_min_rr = 1.2
        bot.execution_enabled = True
        bot.max_orders_per_run = 0
        bot.max_orders_per_cycle = 0
        bot.orders_submitted_this_run = 0
        bot.orders_submitted_this_cycle = 0
        bot.max_order_notional_usd = 0.0
        bot.db = mock.Mock()
        bot.db.get_today_pnl_usd.return_value = 0.0
        bot.ex = mock.Mock()
        bot.ex.get_active_positions.return_value = []
        bot.ex.can_open_new_trade.return_value = True
        bot.ex.get_available_balance.return_value = 1000.0
        bot.risk_manager = mock.Mock()
        bot.risk_manager.check_safety_filters.return_value = (True, "SAFE")
        bot.risk_manager.calculate_lot_size.return_value = (1000.0, 0.156)
        bot.executor = mock.Mock()
        bot.executor.execute_institutional_entry.return_value = {"retCode": 0}
        bot.audit = mock.Mock()
        bot.strategy_journal = mock.Mock()
        bot.filters = types.SimpleNamespace(
            last_adx=12.0,
            last_er=0.2,
            last_atr_pct=0.5,
            last_rel_vol=0.8,
        )
        bot.notifier = mock.Mock()

        result = bot._maybe_execute_read_only_strategy(
            "WIFUSDT",
            self._alt_strategy_result(),
            {"max_open_trades": 1, "risk_per_trade_pct": 1.0},
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "ALT_EXECUTED")
        self.assertEqual(bot.orders_submitted_this_run, 1)
        call_kwargs = bot.executor.execute_institutional_entry.call_args.kwargs
        self.assertEqual(call_kwargs["symbol"], "WIFUSDT")
        self.assertEqual(call_kwargs["side"], "SHORT")
        self.assertEqual(call_kwargs["order_type"], "Limit")
        self.assertEqual(call_kwargs["limit_price"], 0.154)
        self.assertEqual(call_kwargs["tp_levels_override"], {"tp1": 0.148})
        self.assertEqual(call_kwargs["poi"]["type"], "ALT_MEAN_REVERSION")
        self.assertEqual(call_kwargs["sl"], 0.156)
        self.assertEqual(call_kwargs["strategy"], "MEAN_REVERSION")
        self.assertEqual(call_kwargs["source"], "ALT_STRATEGY")
        bot.notifier.notify_signal.assert_called_once()
        event_type, symbol, payload = bot.strategy_journal.record.call_args.args
        self.assertEqual(event_type, "ALT_STRATEGY_EXECUTION")
        self.assertEqual(symbol, "WIFUSDT")
        self.assertTrue(payload["order_submitted"])

    def test_main_multi_strategy_execution_applies_strategy_policy_caps(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.multi_strategy_execution_enabled = True
        bot.multi_strategy_allowed_strategies = {"MEAN_REVERSION"}
        bot.multi_strategy_min_rr = 1.2
        bot.multi_strategy_execution_policy = {
            "MEAN_REVERSION": {
                "min_rr": 1.2,
                "max_notional_usd": 10.0,
                "risk_pct_multiplier": 0.5,
                "cooldown_minutes": 60.0,
                "max_hold_minutes": 120.0,
                "allowed_order_types": ["LIMIT"],
            }
        }
        bot.strategy_execution_last_submit = {}
        bot.execution_enabled = True
        bot.max_orders_per_run = 0
        bot.max_orders_per_cycle = 0
        bot.orders_submitted_this_run = 0
        bot.orders_submitted_this_cycle = 0
        bot.max_order_notional_usd = 0.0
        bot.db = mock.Mock()
        bot.db.get_today_pnl_usd.return_value = 0.0
        bot.ex = mock.Mock()
        bot.ex.get_active_positions.return_value = []
        bot.ex.can_open_new_trade.return_value = True
        bot.ex.get_available_balance.return_value = 1000.0
        bot.risk_manager = mock.Mock()
        bot.risk_manager.check_safety_filters.return_value = (True, "SAFE")
        bot.risk_manager.calculate_lot_size.return_value = (1000.0, 0.156)
        bot.executor = mock.Mock()
        bot.executor.execute_institutional_entry.return_value = {"retCode": 0}
        bot.audit = mock.Mock()
        bot.strategy_journal = mock.Mock()
        bot.filters = types.SimpleNamespace(
            last_adx=12.0,
            last_er=0.2,
            last_atr_pct=0.5,
            last_rel_vol=0.8,
        )
        bot.notifier = mock.Mock()

        result = bot._maybe_execute_read_only_strategy(
            "WIFUSDT",
            self._alt_strategy_result(),
            {"max_open_trades": 1, "risk_per_trade_pct": 1.0},
        )

        self.assertEqual(result["status"], "ALT_EXECUTED")
        call_kwargs = bot.executor.execute_institutional_entry.call_args.kwargs
        self.assertAlmostEqual(call_kwargs["qty"], 10.0 / 0.154)
        self.assertAlmostEqual(call_kwargs["risk_pct"], 0.5)
        self.assertEqual(call_kwargs["max_hold_minutes"], 120.0)
        self.assertIn(("WIFUSDT", "MEAN_REVERSION"), bot.strategy_execution_last_submit)

        self.assertIsNone(
            bot._maybe_execute_read_only_strategy(
                "WIFUSDT",
                self._alt_strategy_result(),
                {"max_open_trades": 1, "risk_per_trade_pct": 1.0},
            )
        )
        self.assertEqual(bot.executor.execute_institutional_entry.call_count, 1)

    def test_main_multi_strategy_health_guard_blocks_problem_strategy(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.multi_strategy_execution_enabled = True
        bot.multi_strategy_allowed_strategies = {"MEAN_REVERSION"}
        bot.multi_strategy_min_rr = 1.2
        bot.multi_strategy_health_guard_enabled = True
        bot.multi_strategy_health_window_minutes = 240.0
        bot.multi_strategy_health_max_rejections = 1
        bot.multi_strategy_health_max_executor_failures = 0
        bot.strategy_execution_health_events = [
            {
                "ts": datetime.datetime.now(datetime.timezone.utc),
                "symbol": "WIFUSDT",
                "strategy": "MEAN_REVERSION",
                "order_submitted": False,
                "reason": "zero_qty_after_notional_cap",
            }
        ]
        bot.executor = mock.Mock()
        bot.strategy_journal = mock.Mock()

        result = bot._maybe_execute_read_only_strategy(
            "WIFUSDT",
            self._alt_strategy_result(),
            {"max_open_trades": 1, "risk_per_trade_pct": 1.0},
        )

        self.assertIsNone(result)
        bot.executor.execute_institutional_entry.assert_not_called()
        _, _, payload = bot.strategy_journal.record.call_args.args
        self.assertEqual(payload["reason"], "strategy_health_guard:rejection_streak")

    def test_main_multi_strategy_execution_respects_global_execution_guard(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.multi_strategy_execution_enabled = True
        bot.multi_strategy_allowed_strategies = {"MEAN_REVERSION"}
        bot.multi_strategy_min_rr = 1.2
        bot.execution_enabled = False
        bot.max_orders_per_run = 0
        bot.max_orders_per_cycle = 0
        bot.orders_submitted_this_run = 0
        bot.orders_submitted_this_cycle = 0
        bot.executor = mock.Mock()

        result = bot._maybe_execute_read_only_strategy(
            "WIFUSDT",
            self._alt_strategy_result(),
            {"max_open_trades": 1, "risk_per_trade_pct": 1.0},
        )

        self.assertIsNone(result)
        bot.executor.execute_institutional_entry.assert_not_called()

    def test_main_entry_quality_gate_reports_missing_checks(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.require_m5_confirmation = True
        bot.require_pd_alignment = True
        bot.require_liquidity_target = True

        analysis = {
            "trend": "SHORT",
            "m5_ok": False,
            "is_pd_aligned": False,
            "has_liquidity_target": False,
            "has_eql": False,
            "has_ql": False,
        }

        self.assertEqual(
            bot._missing_entry_quality_checks(analysis),
            ["m5", "pd_alignment", "liquidity_target"],
        )

        analysis["has_eql"] = True

        self.assertEqual(
            bot._missing_entry_quality_checks(analysis),
            ["m5", "pd_alignment"],
        )

    def test_main_entry_quality_gate_can_be_disabled(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.require_m5_confirmation = False
        bot.require_pd_alignment = False
        bot.require_liquidity_target = False

        analysis = {
            "trend": "LONG",
            "m5_ok": False,
            "is_pd_aligned": False,
            "has_liquidity_target": False,
            "has_eqh": False,
        }

        self.assertEqual(bot._missing_entry_quality_checks(analysis), [])

    def test_main_read_only_strategy_analyzer_can_be_disabled(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.multi_strategy_read_only = False
        bot.regime_classifier = mock.Mock()

        self.assertIsNone(bot._analyze_read_only_strategies("BTCUSDT", {}))
        bot.regime_classifier.analyze.assert_not_called()

    def test_main_read_only_strategy_analyzer_uses_existing_market_data(self):
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.logger = logging.getLogger("test.InstitutionalBot")
        bot.multi_strategy_read_only = True
        bot.regime_classifier = mock.Mock()
        bot.regime_classifier.analyze.return_value = {"regime": "RANGE", "confidence": 80}

        mean_reversion = {"strategy": "MEAN_REVERSION", "status": "DISABLED"}
        breakout = {"strategy": "BREAKOUT", "status": "DISABLED"}
        trend_pullback = {"strategy": "TREND_PULLBACK", "status": "DISABLED"}
        volatility_expansion = {"strategy": "VOLATILITY_EXPANSION", "status": "DISABLED"}
        decision = {
            "decision": "NO_ACTION",
            "reason": "no_strategy_candidate",
            "selected_strategy": None,
        }

        bot.mean_reversion_strategy = mock.Mock()
        bot.mean_reversion_strategy.analyze.return_value = mean_reversion
        bot.breakout_strategy = mock.Mock()
        bot.breakout_strategy.analyze.return_value = breakout
        bot.trend_pullback_strategy = mock.Mock()
        bot.trend_pullback_strategy.analyze.return_value = trend_pullback
        bot.volatility_expansion_strategy = mock.Mock()
        bot.volatility_expansion_strategy.analyze.return_value = volatility_expansion
        bot.read_only_strategy_coordinator = mock.Mock()
        bot.read_only_strategy_coordinator.decide.return_value = decision
        bot.strategy_journal = mock.Mock()

        data = {"1h": object(), "15m": object(), "5m": object()}
        result = bot._analyze_read_only_strategies("BTCUSDT", data)

        self.assertEqual(result["decision"], decision)
        bot.regime_classifier.analyze.assert_called_once_with(data)
        bot.mean_reversion_strategy.analyze.assert_called_once_with(
            symbol="BTCUSDT",
            regime_result={"regime": "RANGE", "confidence": 80},
            df_15m=data["15m"],
            df_5m=data["5m"],
        )
        bot.read_only_strategy_coordinator.decide.assert_called_once()
        bot.strategy_journal.record.assert_not_called()

    def test_main_records_read_only_strategy_observation_for_watch(self):
        import config
        InstitutionalBot = self._import_institutional_bot()

        bot = InstitutionalBot.__new__(InstitutionalBot)
        bot.execution_enabled = False
        bot.strategy_journal = mock.Mock()

        with (
            mock.patch.object(config, "BYBIT_DEMO", True),
            mock.patch.object(config, "BYBIT_TESTNET", False),
        ):
            bot._record_read_only_strategy_observation(
                "WIFUSDT",
                {
                    "regime": "RANGE",
                    "confidence": 72.5,
                    "decision": {
                        "decision": "WATCH_ONLY",
                        "reason": "single_read_only_candidate",
                        "selected_strategy": "MEAN_REVERSION",
                        "side": "SHORT",
                        "score": 85,
                        "threshold": 55,
                        "candidate_count": 1,
                        "candidate_strategies": ["MEAN_REVERSION"],
                        "plan": {
                            "order_type": "Limit",
                            "entry": 0.154,
                            "stop_loss": 0.156,
                            "target": 0.148,
                            "rr": 3.0,
                        },
                    },
                },
            )

        event_type, symbol, payload = bot.strategy_journal.record.call_args.args
        self.assertEqual(event_type, "ALT_STRATEGY_DECISION")
        self.assertEqual(symbol, "WIFUSDT")
        self.assertEqual(payload["decision"], "WATCH_ONLY")
        self.assertEqual(payload["selected_strategy"], "MEAN_REVERSION")
        self.assertFalse(payload["execution_enabled"])
        self.assertTrue(payload["demo"])
        self.assertEqual(payload["plan"]["rr"], 3.0)

    def test_main_read_only_strategy_summary_reports_actionable_states(self):
        InstitutionalBot = self._import_institutional_bot()

        watch_summary = InstitutionalBot._build_read_only_strategy_summary(
            "OPUSDT",
            {
                "decision": {
                    "decision": "WATCH_ONLY",
                    "selected_strategy": "MEAN_REVERSION",
                    "side": "SHORT",
                    "score": 82,
                }
            },
            rel_vol=0.7,
        )
        conflict_summary = InstitutionalBot._build_read_only_strategy_summary(
            "OPUSDT",
            {
                "decision": {
                    "decision": "CONFLICT_NO_ACTION",
                    "candidate_strategies": ["MEAN_REVERSION", "VOLATILITY_EXPANSION"],
                }
            },
            rel_vol=0.8,
        )
        rejected_summary = InstitutionalBot._build_read_only_strategy_summary(
            "OPUSDT",
            {
                "decision": {
                    "decision": "NO_ACTION",
                    "rejected_candidate_count": 1,
                }
            },
            rel_vol=0.9,
        )

        self.assertEqual(watch_summary["status"], "ALT_WATCH")
        self.assertEqual(watch_summary["side"], "SHORT")
        self.assertEqual(watch_summary["rel_vol"], 0.7)
        self.assertEqual(conflict_summary["status"], "ALT_CONFLICT")
        self.assertEqual(rejected_summary["status"], "ALT_REJECT")

    def test_main_read_only_strategy_summary_reports_range_regime_context(self):
        InstitutionalBot = self._import_institutional_bot()

        summary = InstitutionalBot._build_read_only_strategy_summary(
            "WIFUSDT",
            {
                "regime": "RANGE",
                "confidence": 71,
                "reason": "bounded_range_with_repeated_edges",
                "trade_posture": "EDGE_ONLY",
                "metrics": {
                    "range_position": 0.52,
                    "range_width_pct": 2.41,
                    "relative_volume": 0.68,
                    "adx": 16.2,
                },
                "setup": {
                    "status": "RANGE_MID_NO_TRADE",
                    "reason": "price_not_at_range_edge",
                    "range_position": 0.52,
                },
                "mean_reversion": {
                    "strategy": "MEAN_REVERSION",
                    "status": "DISABLED",
                    "reason": "not_at_range_edge",
                    "failed_checks": ["range_edge"],
                    "score": 0,
                    "threshold": 70,
                },
                "decision": {
                    "decision": "NO_ACTION",
                    "reason": "no_strategy_candidate",
                    "rejected_candidate_count": 0,
                },
            },
            rel_vol=0.68,
        )

        self.assertEqual(summary["status"], "ALT_REGIME")
        self.assertEqual(summary["regime"], "RANGE")
        self.assertEqual(summary["setup_status"], "RANGE_MID_NO_TRADE")
        self.assertEqual(summary["range_position"], 0.52)
        self.assertEqual(summary["strategy_states"][0]["reason"], "not_at_range_edge")

    def test_market_summary_reports_alt_watch_separately(self):
        from core.notifier import TelegramNotifier

        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.alerts = {"entry": True}
        notifier.SAFE_LIMIT = 3900
        notifier.send_message = mock.Mock()

        notifier.notify_market_summary(
            [
                {
                    "symbol": "OPUSDT",
                    "status": "ALT_WATCH",
                    "side": "SHORT",
                    "score": 82,
                    "reason": "MEAN_REVERSION read-only candidate",
                    "rel_vol": 0.7,
                }
            ],
            equity=1000.0,
        )

        message = notifier.send_message.call_args.args[0]
        self.assertIn("READ-ONLY ALT WATCH", message)
        self.assertNotIn("FILTERED OUT", message)

    def test_market_summary_reports_regime_map_for_sideways_context(self):
        from core.notifier import TelegramNotifier

        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.alerts = {"entry": True}
        notifier.SAFE_LIMIT = 3900
        notifier.send_message = mock.Mock()

        notifier.notify_market_summary(
            [
                {
                    "symbol": "WIFUSDT",
                    "status": "ALT_REGIME",
                    "regime": "RANGE",
                    "regime_confidence": 71,
                    "trade_posture": "EDGE_ONLY",
                    "setup_status": "RANGE_MID_NO_TRADE",
                    "setup_reason": "price_not_at_range_edge",
                    "range_position": 0.52,
                    "range_width_pct": 2.41,
                    "relative_volume": 0.68,
                    "reason": "price_not_at_range_edge",
                }
            ],
            equity=1000.0,
        )

        message = notifier.send_message.call_args.args[0]
        self.assertIn("REGIME MAP", message)
        self.assertIn("RANGE_MID_NO_TRADE", message)
        self.assertIn("pos=0.52", message)
        self.assertIn("price_not_at_range_edge", message)

    def test_market_summary_reports_alt_executed_separately(self):
        from core.notifier import TelegramNotifier

        notifier = TelegramNotifier.__new__(TelegramNotifier)
        notifier.alerts = {"entry": True}
        notifier.SAFE_LIMIT = 3900
        notifier.send_message = mock.Mock()

        notifier.notify_market_summary(
            [
                {
                    "symbol": "WIFUSDT",
                    "status": "ALT_EXECUTED",
                    "side": "SHORT",
                    "score": 82,
                    "reason": "MEAN_REVERSION submitted",
                    "rel_vol": 0.7,
                }
            ],
            equity=1000.0,
        )

        message = notifier.send_message.call_args.args[0]
        self.assertIn("ALT STRATEGY EXECUTED", message)
        self.assertIn("MEAN_REVERSION submitted", message)
        self.assertNotIn("FILTERED OUT", message)


if __name__ == "__main__":
    unittest.main()
