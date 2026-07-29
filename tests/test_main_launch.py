import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.preflight_main import run_preflight
from scripts.run_main_profile import (
    apply_numeric_overrides,
    build_log_path,
    build_profile_env,
    parse_env_override,
    pid_lock,
    run_preflight as run_launcher_preflight,
)


class MainLaunchProfileTests(unittest.TestCase):
    def test_profiles_apply_safe_execution_defaults(self):
        observe = build_profile_env("observe", base_env={})
        demo_multi = build_profile_env("demo-multi", base_env={})

        self.assertEqual(observe["BYBIT_DEMO"], "true")
        self.assertEqual(observe["EXECUTION_ENABLED"], "false")
        self.assertEqual(observe["MULTI_STRATEGY_EXECUTION_ENABLED"], "false")

        self.assertEqual(demo_multi["BYBIT_DEMO"], "true")
        self.assertEqual(demo_multi["EXECUTION_ENABLED"], "true")
        self.assertEqual(demo_multi["MULTI_STRATEGY_EXECUTION_ENABLED"], "true")
        self.assertEqual(demo_multi["MAX_ORDERS_PER_RUN"], "1")
        self.assertEqual(demo_multi["MAX_ORDERS_PER_CYCLE"], "1")
        self.assertEqual(demo_multi["MULTI_STRATEGY_HEALTH_GUARD_ENABLED"], "true")
        self.assertEqual(demo_multi["MULTI_STRATEGY_HEALTH_MAX_REJECTIONS"], "3")

    def test_numeric_and_env_overrides_are_explicit(self):
        env = build_profile_env(
            "demo-smc",
            base_env={},
            overrides=["MAX_ORDER_NOTIONAL_USD=12.5"],
        )
        apply_numeric_overrides(
            env,
            runtime_minutes=45,
            max_orders_per_run=2,
            max_orders_per_cycle=1,
        )

        self.assertEqual(env["MAX_ORDER_NOTIONAL_USD"], "12.5")
        self.assertEqual(env["MAX_RUNTIME_MINUTES"], "45")
        self.assertEqual(env["MAX_ORDERS_PER_RUN"], "2")
        self.assertEqual(env["MAX_ORDERS_PER_CYCLE"], "1")

    def test_rejects_invalid_env_override_key(self):
        with self.assertRaises(ValueError):
            parse_env_override("bad-key=value")

    def test_pid_lock_rejects_running_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "main.pid"
            path.write_text(f"{os.getpid()}\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "already appears to be running"):
                with pid_lock(path):
                    pass

    def test_build_log_path_uses_logs_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = build_log_path(Path(tmpdir), "demo-smc")

            self.assertEqual(path.parent, Path(tmpdir) / "logs")
            self.assertIn("main_demo-smc_", path.name)

    def test_preflight_accepts_guarded_demo_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._fake_config(
                tmpdir,
                demo=True,
                testnet=False,
                execution_enabled=True,
                multi_strategy_execution_enabled=False,
            )

            with patch.dict(sys.modules, {"config": cfg}):
                result = run_preflight(profile="demo-smc")

            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["environment"]["demo"], True)
            self.assertEqual(result["guards"]["max_orders_per_run"], 1)

    def test_preflight_refuses_live_without_allow_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._fake_config(
                tmpdir,
                demo=False,
                testnet=False,
                allow_live_trading=False,
            )

            with patch.dict(sys.modules, {"config": cfg}):
                result = run_preflight(profile="live")

            self.assertEqual(result["status"], "FAILED")
            self.assertIn("live_mode_refused_without_ALLOW_LIVE_TRADING", result["errors"])

    def test_exchange_preflight_blocks_unsafe_existing_exchange_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._fake_config(
                tmpdir,
                demo=True,
                testnet=False,
                execution_enabled=True,
                multi_strategy_execution_enabled=True,
            )

            class FakeExchange:
                def get_total_balance(self):
                    return 1000.0

                def get_active_positions(self):
                    return [
                        {
                            "symbol": "INJUSDT",
                            "side": "Sell",
                            "positionIdx": 2,
                            "stopLoss": 0,
                        },
                        {
                            "symbol": "SEIUSDT",
                            "side": "Sell",
                            "positionIdx": 2,
                            "stopLoss": 0.0447,
                        },
                    ]

                def get_pending_entry_orders(self):
                    return [
                        {
                            "symbol": "XRPUSDT",
                            "orderId": "large-order",
                            "leavesValue": "275.8137",
                        },
                        {
                            "symbol": "WIFUSDT",
                            "orderId": "small-order",
                            "leavesValue": "14.7901",
                        },
                    ]

            exchange_module = types.ModuleType("core.exchange")
            exchange_module.ExchangeManager = FakeExchange

            with patch.dict(sys.modules, {"config": cfg, "core.exchange": exchange_module}):
                result = run_preflight(profile="demo-multi", exchange_check=True)

            self.assertEqual(result["status"], "FAILED")
            self.assertIn(
                "active_position_without_stop_loss:INJUSDT:Sell:positionIdx=2",
                result["errors"],
            )
            self.assertIn(
                "pending_entry_order_exceeds_max_notional:"
                "XRPUSDT:large-order:275.8137>25.0000",
                result["errors"],
            )
            self.assertNotIn(
                "pending_entry_order_exceeds_max_notional:"
                "WIFUSDT:small-order:14.7901>25.0000",
                result["errors"],
            )

    def test_exchange_preflight_can_repair_unsafe_demo_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = self._fake_config(
                tmpdir,
                demo=True,
                testnet=False,
                execution_enabled=True,
                multi_strategy_execution_enabled=True,
            )

            class FakeExchange:
                CATEGORY = "linear"

                def __init__(self):
                    self.session = self
                    self.stop_set = False
                    self.cancelled = False

                def get_total_balance(self):
                    return 1000.0

                def get_ticker_info(self, symbol):
                    return {"tickSize": 0.0001}

                def get_active_positions(self):
                    return [
                        {
                            "symbol": "INJUSDT",
                            "side": "Sell",
                            "positionIdx": 2,
                            "entryPrice": 0,
                            "markPrice": 4.6285,
                            "stopLoss": 4.7211 if self.stop_set else 0,
                        }
                    ]

                def get_pending_entry_orders(self):
                    if self.cancelled:
                        return []
                    return [
                        {
                            "symbol": "XRPUSDT",
                            "orderId": "large-order",
                            "leavesValue": "275.8137",
                        }
                    ]

                def _request_with_retry(self, func, **kwargs):
                    return func(**kwargs)

                def set_trading_stop(self, **kwargs):
                    self.stop_set = True
                    return {"retCode": 0, "result": kwargs}

                def cancel_order(self, **kwargs):
                    self.cancelled = True
                    return {"retCode": 0, "result": kwargs}

            exchange_module = types.ModuleType("core.exchange")
            exchange_module.ExchangeManager = FakeExchange

            with patch.dict(
                sys.modules,
                {"config": cfg, "core.exchange": exchange_module},
            ), patch.dict(os.environ, {"PREFLIGHT_REPAIR_EXCHANGE_STATE": "true"}):
                result = run_preflight(profile="demo-multi", exchange_check=True)

            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["errors"], [])
            self.assertEqual(len(result["exchange"]["repair_actions"]), 2)
            self.assertTrue(
                all(action["status"] == "OK" for action in result["exchange"]["repair_actions"])
            )

    def test_launcher_preflight_can_request_exchange_check(self):
        calls: list[list[str]] = []

        def fake_run(command, cwd, env, check):
            calls.append(command)

        with patch("scripts.run_main_profile.subprocess.run", side_effect=fake_run):
            run_launcher_preflight(
                Path("/repo"),
                "/python",
                "demo-smc",
                {},
                exchange_check=True,
            )

        self.assertEqual(calls[0], ["/python", "scripts/preflight_main.py", "--profile", "demo-smc", "--exchange"])

    def test_launcher_preflight_can_request_exchange_repair(self):
        calls: list[list[str]] = []

        def fake_run(command, cwd, env, check):
            calls.append(command)

        with patch("scripts.run_main_profile.subprocess.run", side_effect=fake_run):
            run_launcher_preflight(
                Path("/repo"),
                "/python",
                "demo-multi",
                {},
                exchange_check=True,
                repair_exchange_state=True,
            )

        self.assertEqual(
            calls[0],
            [
                "/python",
                "scripts/preflight_main.py",
                "--profile",
                "demo-multi",
                "--exchange",
                "--repair-exchange-state",
            ],
        )

    def _fake_config(
        self,
        tmpdir: str,
        *,
        demo: bool,
        testnet: bool,
        execution_enabled: bool = True,
        multi_strategy_execution_enabled: bool = False,
        allow_live_trading: bool = False,
    ):
        root = Path(tmpdir)
        return types.SimpleNamespace(
            API_KEY="demo-key",
            API_SECRET="demo-secret",
            BYBIT_DEMO=demo,
            BYBIT_TESTNET=testnet,
            DATA_DIR=root / "data",
            LOGS_DIR=root / "logs",
            DB_PATH=root / "data" / "bot_memory.db",
            LOG_PATH=root / "logs" / "bot.log",
            STRATEGY_OBSERVATION_PATH=root / "logs" / "strategy_observations.jsonl",
            RISK_MANAGEMENT={
                "global": {
                    "allow_live_trading": allow_live_trading,
                    "execution_enabled": execution_enabled,
                    "multi_strategy_execution_enabled": multi_strategy_execution_enabled,
                    "multi_strategy_health_guard_enabled": True,
                    "multi_strategy_health_window_minutes": 240,
                    "multi_strategy_health_max_rejections": 3,
                    "multi_strategy_health_max_executor_failures": 1,
                    "max_runtime_minutes": 30,
                    "max_orders_per_run": 1,
                    "max_orders_per_cycle": 1,
                    "max_order_notional_usd": 25,
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
