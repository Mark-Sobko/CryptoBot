#!/usr/bin/env python3
"""Run local safety checks before accepting a commit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.secret_scan import Finding, is_blocked_runtime_path, scan_blob_text


COMPILE_TARGETS = (
    "core",
    "engine",
    "tests",
    "scripts",
    "main.py",
    "config.py",
    "analyze_trades.py",
)


def run_command(label: str, command: list[str]) -> int:
    print(f"PRE_COMMIT_STEP {label}")
    result = subprocess.run(command, cwd=ROOT_DIR)
    if result.returncode != 0:
        print(f"PRE_COMMIT_FAILED {label}", file=sys.stderr)
    return int(result.returncode)


def run_git_bytes(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT_DIR,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def split_nul(data: bytes) -> list[str]:
    return [item.decode("utf-8", errors="replace") for item in data.split(b"\0") if item]


def staged_paths() -> list[str]:
    result = run_git_bytes(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
    return split_nul(result.stdout)


def read_staged_blob(path: str) -> str:
    result = run_git_bytes(["show", f":{path}"], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="ignore")


def scan_staged_changes(
    paths: Iterable[str],
    blob_reader: Callable[[str], str] = read_staged_blob,
) -> list[Finding]:
    findings: list[Finding] = []

    for path in paths:
        if is_blocked_runtime_path(path):
            findings.append(
                Finding(
                    "runtime-path",
                    path,
                    "staged blocked runtime/secret path",
                )
            )

        findings.extend(scan_blob_text(blob_reader(path), f"staged:{path}"))

    return findings


def check_staged_changes() -> int:
    findings = scan_staged_changes(staged_paths())
    if not findings:
        print("PRE_COMMIT_STAGED_SCAN_OK")
        return 0

    print("PRE_COMMIT_STAGED_SCAN_FAILED", file=sys.stderr)
    for finding in findings:
        print(f"{finding.kind}: {finding.location}: {finding.detail}", file=sys.stderr)
    return 1


def existing_compile_targets() -> list[str]:
    return [target for target in COMPILE_TARGETS if (ROOT_DIR / target).exists()]


def main() -> int:
    python = sys.executable

    steps: list[tuple[str, Callable[[], int]]] = [
        ("staged secret/runtime scan", check_staged_changes),
        (
            "full history secret/runtime scan",
            lambda: run_command(
                "full history secret/runtime scan",
                [python, "scripts/secret_scan.py", "--history"],
            ),
        ),
        (
            "compile Python sources",
            lambda: run_command(
                "compile Python sources",
                [python, "-m", "compileall", "-q", *existing_compile_targets()],
            ),
        ),
        (
            "unit tests",
            lambda: run_command(
                "unit tests",
                [python, "-m", "unittest", "discover", "-s", "tests", "-v"],
            ),
        ),
    ]

    for label, step in steps:
        code = step()
        if code != 0:
            print(f"PRE_COMMIT_STOPPED {label}", file=sys.stderr)
            return code

    print("PRE_COMMIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
