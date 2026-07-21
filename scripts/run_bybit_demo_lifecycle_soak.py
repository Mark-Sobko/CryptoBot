#!/usr/bin/env python3
"""Run repeated Bybit demo/testnet lifecycle checks.

This wrapper delegates each iteration to run_bybit_demo_lifecycle.py so the
same demo/testnet fail-closed guard and cleanup behavior are reused.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
LIFECYCLE_SCRIPT = ROOT_DIR / "scripts" / "run_bybit_demo_lifecycle.py"

REQUIRED_STEPS = (
    "limit_create",
    "limit_amend",
    "retcode_matrix",
    "limit_cancel",
    "market_open",
    "reduce_only_tp_create",
    "stop_loss_set",
    "restart_recovery_sync",
    "reduce_only_tp_cancel",
    "stop_loss_clear",
    "market_close_reduce_only",
)


def parse_json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    idx = 0

    while idx < len(text):
        start = text.find("{", idx)
        if start == -1:
            break

        try:
            parsed, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            idx = start + 1
            continue

        if isinstance(parsed, dict):
            objects.append(parsed)
        idx = start + end

    return objects


def step_names(summary: dict[str, Any]) -> set[str]:
    return {
        str(step.get("name", ""))
        for step in summary.get("steps", [])
        if isinstance(step, dict)
    }


def validate_lifecycle_summary(
    summary: dict[str, Any],
    *,
    require_partial_close: bool,
) -> list[str]:
    errors: list[str] = []

    if summary.get("status") != "OK":
        errors.append(f"status={summary.get('status')!r}")

    names = step_names(summary)
    required = list(REQUIRED_STEPS)
    if require_partial_close:
        required.append("partial_reduce_only_close")

    missing = [name for name in required if name not in names]
    if missing:
        errors.append(f"missing_steps={missing}")

    recovery_steps = [
        step
        for step in summary.get("steps", [])
        if isinstance(step, dict) and step.get("name") == "restart_recovery_sync"
    ]
    if not recovery_steps:
        errors.append("restart_recovery_sync missing")
    elif recovery_steps[-1].get("status") != "OPEN":
        errors.append(f"restart_recovery_sync status={recovery_steps[-1].get('status')!r}")

    return errors


def build_lifecycle_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(LIFECYCLE_SCRIPT),
        "--symbol",
        args.symbol,
        "--max-notional",
        str(args.max_notional),
        "--wait",
        str(args.wait),
    ]

    if args.skip_partial_fill_probe:
        command.append("--skip-partial-fill-probe")
    if args.skip_partial_close:
        command.append("--skip-partial-close")

    return command


def run_iteration(index: int, args: argparse.Namespace) -> dict[str, Any]:
    command = build_lifecycle_command(args)
    started_at = time.time()
    result = subprocess.run(
        command,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    objects = parse_json_objects(result.stdout)
    summary = objects[0] if objects else {}
    validation_errors = validate_lifecycle_summary(
        summary,
        require_partial_close=not args.skip_partial_close,
    )

    run = {
        "iteration": index,
        "returncode": result.returncode,
        "duration_s": round(time.time() - started_at, 3),
        "command": command,
        "summary": summary,
        "stderr": result.stderr.strip(),
        "validation_errors": validation_errors,
        "ok": result.returncode == 0 and not validation_errors,
    }

    if not objects:
        run["parse_error"] = "child process did not emit a JSON summary"

    return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--symbol", default="XRPUSDT")
    parser.add_argument("--max-notional", default="25")
    parser.add_argument("--wait", type=float, default=20.0)
    parser.add_argument("--sleep", type=float, default=3.0)
    parser.add_argument("--skip-partial-fill-probe", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-partial-close", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()

    if args.iterations <= 0:
        raise SystemExit("--iterations must be positive")

    runs: list[dict[str, Any]] = []
    for index in range(1, args.iterations + 1):
        run = run_iteration(index, args)
        runs.append(run)
        if index < args.iterations and run["ok"]:
            time.sleep(max(args.sleep, 0.0))
        if not run["ok"] and not args.continue_on_error:
            break

    failed = [run for run in runs if not run["ok"]]
    output = {
        "status": "ERROR" if failed else "OK",
        "iterations_requested": args.iterations,
        "iterations_completed": len(runs),
        "successful": len(runs) - len(failed),
        "failed": len(failed),
        "symbol": args.symbol.upper(),
        "skip_partial_fill_probe": args.skip_partial_fill_probe,
        "runs": runs,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
