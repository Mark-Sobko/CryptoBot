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


if __name__ == "__main__":
    unittest.main()
