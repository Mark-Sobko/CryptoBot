#!/usr/bin/env python3
"""Fail CI when secrets or runtime artifacts are tracked by git."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SECRET_RE = re.compile(
    r"(?im)"
    r"(^[ \t]*(BYBIT_API_KEY|BYBIT_API_SECRET|TELEGRAM_TOKEN|TELEGRAM_CHAT_ID)[ \t]*=[ \t]*['\"]?[^'\"\s#][^'\"#\r\n]*)"
    r"|(\b\d{8,10}:[A-Za-z0-9_-]{35,}\b)"
    r"|(\bgithub_pat_[A-Za-z0-9_]{20,}\b)"
    r"|(\bgh[pousr]_[A-Za-z0-9]{36,}\b)"
    r"|(-----BEGIN (RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----)"
)


@dataclass(frozen=True)
class Finding:
    kind: str
    location: str
    detail: str


def run_git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run_git_bytes(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def is_blocked_runtime_path(path: str) -> bool:
    parts = [part for part in Path(path).parts if part not in ("", ".")]
    if not parts:
        return False

    name = parts[-1]

    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True

    if any(part in {"data", "logs"} for part in parts):
        return True

    if any("pycache" in part.lower() for part in parts):
        return True

    if name in {".DS_Store", "deploy_log.txt"}:
        return True

    blocked_suffixes = (".log", ".pyc", ".pyo")
    if name.endswith(blocked_suffixes):
        return True

    return ".db" in name or ".sqlite" in name


def tracked_files() -> list[str]:
    result = run_git(["ls-files", "-z"])
    return [item for item in result.stdout.split("\0") if item]


def history_files() -> list[str]:
    result = run_git(["log", "--all", "--name-only", "--pretty=format:"])
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def scan_paths(paths: list[str], scope: str) -> list[Finding]:
    return [
        Finding("runtime-path", path, f"{scope}: blocked runtime/secret path is tracked")
        for path in paths
        if is_blocked_runtime_path(path)
    ]


def scan_blob_text(text: str, location: str) -> list[Finding]:
    findings: list[Finding] = []
    for match in SECRET_RE.finditer(text):
        snippet = match.group(0).strip()
        key = snippet.split("=", 1)[0].strip() if "=" in snippet else "token-pattern"
        findings.append(Finding("secret-pattern", location, key))
    return findings


def scan_current_content(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        try:
            data = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings.extend(scan_blob_text(data, path))
    return findings


def scan_history_content() -> list[Finding]:
    revs = run_git(["rev-list", "--all"]).stdout.splitlines()
    findings: list[Finding] = []

    for rev in revs:
        tree = run_git(["ls-tree", "-r", "--name-only", rev]).stdout.splitlines()
        for path in tree:
            blob = run_git_bytes(["show", f"{rev}:{path}"], check=False)
            if blob.returncode != 0:
                continue
            text = blob.stdout.decode("utf-8", errors="ignore")
            for finding in scan_blob_text(text, f"{rev[:12]}:{path}"):
                findings.append(finding)

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="scan all commits, not only current files")
    args = parser.parse_args()

    findings: list[Finding] = []
    current_paths = tracked_files()
    findings.extend(scan_paths(current_paths, "current"))
    findings.extend(scan_current_content(current_paths))

    if args.history:
        findings.extend(scan_paths(history_files(), "history"))
        findings.extend(scan_history_content())

    if findings:
        print("SECRET_SCAN_FAILED", file=sys.stderr)
        for finding in findings:
            print(
                f"{finding.kind}: {finding.location}: {finding.detail}",
                file=sys.stderr,
            )
        return 1

    print("SECRET_SCAN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
