from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]

    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass

    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass

    return str(value)


class StrategyObservationJournal:
    """Writes compact strategy observations to JSONL without affecting execution."""

    def __init__(self, path: str | Path, *, enabled: bool = True) -> None:
        self.path = str(path)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

        if self.enabled and self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def record(self, event_type: str, symbol: str, data: Mapping[str, Any]) -> bool:
        if not self.enabled or not self.path:
            return False

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": str(event_type),
            "symbol": str(symbol),
            "data": _json_safe(data if isinstance(data, Mapping) else {"value": data}),
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            return True
        except Exception as exc:
            print(f"[STRATEGY JOURNAL ERROR] JSONL write failed: {exc}", file=sys.stderr)
            return False
