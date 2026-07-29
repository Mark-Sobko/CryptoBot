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
