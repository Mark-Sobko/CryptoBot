from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "observe": {
        "BYBIT_DEMO": "true",
        "BYBIT_TESTNET": "false",
        "EXECUTION_ENABLED": "false",
        "MULTI_STRATEGY_READ_ONLY": "true",
        "MULTI_STRATEGY_EXECUTION_ENABLED": "false",
        "MAX_RUNTIME_MINUTES": "30",
        "MAX_ORDERS_PER_RUN": "1",
        "MAX_ORDERS_PER_CYCLE": "1",
        "MAX_ORDER_NOTIONAL_USD": "25",
        "MULTI_STRATEGY_HEALTH_GUARD_ENABLED": "true",
        "MULTI_STRATEGY_HEALTH_WINDOW_MINUTES": "240",
        "MULTI_STRATEGY_HEALTH_MAX_REJECTIONS": "3",
        "MULTI_STRATEGY_HEALTH_MAX_EXECUTOR_FAILURES": "1",
    },
    "demo-smc": {
        "BYBIT_DEMO": "true",
        "BYBIT_TESTNET": "false",
        "EXECUTION_ENABLED": "true",
        "MULTI_STRATEGY_READ_ONLY": "true",
        "MULTI_STRATEGY_EXECUTION_ENABLED": "false",
        "MAX_RUNTIME_MINUTES": "30",
        "MAX_ORDERS_PER_RUN": "1",
        "MAX_ORDERS_PER_CYCLE": "1",
        "MAX_ORDER_NOTIONAL_USD": "25",
        "MULTI_STRATEGY_HEALTH_GUARD_ENABLED": "true",
        "MULTI_STRATEGY_HEALTH_WINDOW_MINUTES": "240",
        "MULTI_STRATEGY_HEALTH_MAX_REJECTIONS": "3",
        "MULTI_STRATEGY_HEALTH_MAX_EXECUTOR_FAILURES": "1",
    },
    "demo-multi": {
        "BYBIT_DEMO": "true",
        "BYBIT_TESTNET": "false",
        "EXECUTION_ENABLED": "true",
        "MULTI_STRATEGY_READ_ONLY": "true",
        "MULTI_STRATEGY_EXECUTION_ENABLED": "true",
        "MAX_RUNTIME_MINUTES": "30",
        "MAX_ORDERS_PER_RUN": "1",
        "MAX_ORDERS_PER_CYCLE": "1",
        "MAX_ORDER_NOTIONAL_USD": "25",
        "MULTI_STRATEGY_HEALTH_GUARD_ENABLED": "true",
        "MULTI_STRATEGY_HEALTH_WINDOW_MINUTES": "240",
        "MULTI_STRATEGY_HEALTH_MAX_REJECTIONS": "3",
        "MULTI_STRATEGY_HEALTH_MAX_EXECUTOR_FAILURES": "1",
    },
    "testnet-smc": {
        "BYBIT_DEMO": "false",
        "BYBIT_TESTNET": "true",
        "EXECUTION_ENABLED": "true",
        "MULTI_STRATEGY_READ_ONLY": "true",
        "MULTI_STRATEGY_EXECUTION_ENABLED": "false",
        "MAX_RUNTIME_MINUTES": "30",
        "MAX_ORDERS_PER_RUN": "1",
        "MAX_ORDERS_PER_CYCLE": "1",
        "MAX_ORDER_NOTIONAL_USD": "25",
        "MULTI_STRATEGY_HEALTH_GUARD_ENABLED": "true",
        "MULTI_STRATEGY_HEALTH_WINDOW_MINUTES": "240",
        "MULTI_STRATEGY_HEALTH_MAX_REJECTIONS": "3",
        "MULTI_STRATEGY_HEALTH_MAX_EXECUTOR_FAILURES": "1",
    },
    "testnet-multi": {
        "BYBIT_DEMO": "false",
        "BYBIT_TESTNET": "true",
        "EXECUTION_ENABLED": "true",
        "MULTI_STRATEGY_READ_ONLY": "true",
        "MULTI_STRATEGY_EXECUTION_ENABLED": "true",
        "MAX_RUNTIME_MINUTES": "30",
        "MAX_ORDERS_PER_RUN": "1",
        "MAX_ORDERS_PER_CYCLE": "1",
        "MAX_ORDER_NOTIONAL_USD": "25",
        "MULTI_STRATEGY_HEALTH_GUARD_ENABLED": "true",
        "MULTI_STRATEGY_HEALTH_WINDOW_MINUTES": "240",
        "MULTI_STRATEGY_HEALTH_MAX_REJECTIONS": "3",
        "MULTI_STRATEGY_HEALTH_MAX_EXECUTOR_FAILURES": "1",
    },
}

ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_python(root: Path) -> str:
    venv_python = root / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def parse_env_override(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise ValueError(f"invalid --env override: {raw}")
    key, value = raw.split("=", 1)
    key = key.strip()
    if not ENV_KEY_RE.fullmatch(key):
        raise ValueError(f"invalid env key: {key}")
    return key, value


def build_profile_env(
    profile: str,
    *,
    base_env: dict[str, str] | None = None,
    overrides: list[str] | None = None,
) -> dict[str, str]:
    if profile not in PROFILE_DEFAULTS:
        raise ValueError(f"unknown profile: {profile}")

    env = dict(base_env if base_env is not None else os.environ)
    env.update(PROFILE_DEFAULTS[profile])

    for raw in overrides or []:
        key, value = parse_env_override(raw)
        env[key] = value

    return env


def apply_numeric_overrides(
    env: dict[str, str],
    *,
    runtime_minutes: float | None = None,
    max_order_notional: float | None = None,
    max_orders_per_run: int | None = None,
    max_orders_per_cycle: int | None = None,
) -> None:
    if runtime_minutes is not None:
        env["MAX_RUNTIME_MINUTES"] = str(runtime_minutes)
    if max_order_notional is not None:
        env["MAX_ORDER_NOTIONAL_USD"] = str(max_order_notional)
    if max_orders_per_run is not None:
        env["MAX_ORDERS_PER_RUN"] = str(max_orders_per_run)
    if max_orders_per_cycle is not None:
        env["MAX_ORDERS_PER_CYCLE"] = str(max_orders_per_cycle)


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextmanager
def pid_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raw_pid = path.read_text(encoding="utf-8").strip()
        try:
            old_pid = int(raw_pid)
        except ValueError:
            old_pid = 0
        if process_exists(old_pid):
            raise RuntimeError(f"main.py already appears to be running: pid={old_pid}")
        path.unlink()

    current_pid = os.getpid()
    path.write_text(f"{current_pid}\n", encoding="utf-8")
    try:
        yield
    finally:
        try:
            if path.exists() and path.read_text(encoding="utf-8").strip() == str(current_pid):
                path.unlink()
        except OSError:
            pass


def build_log_path(root: Path, profile: str, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return root / "logs" / f"main_{profile}_{stamp}.log"


def run_preflight(
    root: Path,
    python_bin: str,
    profile: str,
    env: dict[str, str],
    *,
    exchange_check: bool = False,
) -> None:
    command = [python_bin, "scripts/preflight_main.py", "--profile", profile]
    if exchange_check:
        command.append("--exchange")
    subprocess.run(command, cwd=root, env=env, check=True)


def run_main(root: Path, python_bin: str, env: dict[str, str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [python_bin, "main.py"]
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"--- launch {datetime.now(timezone.utc).isoformat()} ---\n")
        log_file.flush()
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log_file.write(line)
            log_file.flush()
        return process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch CryptoBot main.py using a safe profile.")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_DEFAULTS),
        default="observe",
        help="Launch profile. Defaults to observe, which submits no orders.",
    )
    parser.add_argument("--runtime-minutes", type=float)
    parser.add_argument("--max-order-notional", type=float)
    parser.add_argument("--max-orders-per-run", type=int)
    parser.add_argument("--max-orders-per-cycle", type=int)
    parser.add_argument("--env", action="append", default=[], help="Override env as KEY=VALUE.")
    parser.add_argument("--log-file")
    parser.add_argument("--lock-file")
    parser.add_argument("--python")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument(
        "--exchange-preflight",
        action="store_true",
        help="Include Bybit balance/position/order checks in preflight.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = repo_root()
    python_bin = args.python or default_python(root)
    env = build_profile_env(args.profile, overrides=args.env)
    apply_numeric_overrides(
        env,
        runtime_minutes=args.runtime_minutes,
        max_order_notional=args.max_order_notional,
        max_orders_per_run=args.max_orders_per_run,
        max_orders_per_cycle=args.max_orders_per_cycle,
    )

    log_path = build_log_path(root, args.profile, args.log_file)
    lock_path = Path(args.lock_file).expanduser().resolve() if args.lock_file else root / "data" / "main.pid"

    launch_summary = {
        "profile": args.profile,
        "python": python_bin,
        "command": [python_bin, "main.py"],
        "log_file": str(log_path),
        "lock_file": str(lock_path),
        "exchange_preflight": bool(args.exchange_preflight),
        "env": {
            key: env.get(key)
            for key in [
                "BYBIT_DEMO",
                "BYBIT_TESTNET",
                "EXECUTION_ENABLED",
                "MULTI_STRATEGY_READ_ONLY",
                "MULTI_STRATEGY_EXECUTION_ENABLED",
                "MAX_RUNTIME_MINUTES",
                "MAX_ORDERS_PER_RUN",
                "MAX_ORDERS_PER_CYCLE",
                "MAX_ORDER_NOTIONAL_USD",
                "MULTI_STRATEGY_HEALTH_GUARD_ENABLED",
                "MULTI_STRATEGY_HEALTH_WINDOW_MINUTES",
                "MULTI_STRATEGY_HEALTH_MAX_REJECTIONS",
                "MULTI_STRATEGY_HEALTH_MAX_EXECUTOR_FAILURES",
            ]
        },
    }

    print(json.dumps(launch_summary, indent=2, sort_keys=True))
    if args.dry_run:
        return 0

    if not args.skip_preflight:
        run_preflight(
            root,
            python_bin,
            args.profile,
            env,
            exchange_check=args.exchange_preflight,
        )

    with pid_lock(lock_path):
        start = time.monotonic()
        status = run_main(root, python_bin, env, log_path)
        elapsed = time.monotonic() - start

    print(json.dumps({"status": status, "duration_s": round(elapsed, 3), "log_file": str(log_path)}))
    return int(status)


if __name__ == "__main__":
    raise SystemExit(main())
