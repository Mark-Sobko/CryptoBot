from __future__ import annotations

import math
from typing import Any

import pandas as pd

from engine.market_regime import REGIME_RANGE, clamp, safe_float


STRATEGY_NAME = "MEAN_REVERSION"
STATUS_DISABLED = "DISABLED"
STATUS_WAIT_RECLAIM = "WAIT_RECLAIM"
STATUS_WAIT_VOLUME = "WAIT_VOLUME"
STATUS_REJECT_RR = "REJECT_RR"
STATUS_WATCH_ONLY = "WATCH_ONLY"


def _clean_ohlcv(df: pd.DataFrame | None, *, min_len: int) -> pd.DataFrame | None:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None

    required = ["open", "high", "low", "close", "volume"]
    if not set(required).issubset(df.columns):
        return None

    clean = df[required].copy()
    for column in required:
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    clean = clean.dropna()

    if len(clean) >= min_len + 1:
        clean = clean.iloc[:-1].copy()

    if len(clean) < min_len:
        return None
    return clean


def _average_true_range(df: pd.DataFrame, *, length: int = 14) -> float:
    if len(df) < length + 1:
        return 0.0

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return safe_float(true_range.tail(length).mean())


def _candle_metrics(row: pd.Series) -> dict[str, float]:
    open_price = safe_float(row.get("open"))
    high = safe_float(row.get("high"))
    low = safe_float(row.get("low"))
    close = safe_float(row.get("close"))
    candle_range = max(high - low, 0.0)

    if candle_range <= 0:
        return {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "body_ratio": 0.0,
            "upper_wick_ratio": 0.0,
            "lower_wick_ratio": 0.0,
        }

    body = abs(close - open_price)
    upper_wick = max(high - max(open_price, close), 0.0)
    lower_wick = max(min(open_price, close) - low, 0.0)
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "body_ratio": round(body / candle_range, 4),
        "upper_wick_ratio": round(upper_wick / candle_range, 4),
        "lower_wick_ratio": round(lower_wick / candle_range, 4),
    }


def _risk_reward_ratio(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return abs(target - entry) / risk


class MeanReversionStrategy:
    """Read-only range-edge mean reversion candidate detector."""

    def __init__(
        self,
        *,
        min_score: int = 70,
        min_rr: float = 1.2,
        max_trigger_volume_ratio: float = 1.35,
        min_stop_pct: float = 0.18,
    ) -> None:
        self.min_score = min_score
        self.min_rr = min_rr
        self.max_trigger_volume_ratio = max_trigger_volume_ratio
        self.min_stop_pct = min_stop_pct

    def _disabled(
        self,
        *,
        symbol: str,
        reason: str,
        failed_checks: list[str],
    ) -> dict[str, Any]:
        return {
            "strategy": STRATEGY_NAME,
            "symbol": symbol,
            "status": STATUS_DISABLED,
            "read_only": True,
            "execution_disabled": True,
            "score": 0,
            "threshold": self.min_score,
            "side": None,
            "reason": reason,
            "failed_checks": failed_checks,
        }

    def analyze(
        self,
        *,
        symbol: str,
        regime_result: dict[str, Any],
        df_15m: pd.DataFrame | None,
        df_5m: pd.DataFrame | None,
    ) -> dict[str, Any]:
        setup = regime_result.get("setup")
        if regime_result.get("regime") != REGIME_RANGE:
            return self._disabled(
                symbol=symbol,
                reason="not_range_regime",
                failed_checks=["regime"],
            )
        if not isinstance(setup, dict) or setup.get("status") != "RANGE_EDGE_WATCH":
            return self._disabled(
                symbol=symbol,
                reason="not_at_range_edge",
                failed_checks=["range_edge"],
            )

        side = str(setup.get("side", "")).upper()
        if side not in ("LONG", "SHORT"):
            return self._disabled(
                symbol=symbol,
                reason="invalid_range_side",
                failed_checks=["side"],
            )

        metrics = regime_result.get("metrics") if isinstance(regime_result.get("metrics"), dict) else {}
        range_high = safe_float(metrics.get("range_high"))
        range_low = safe_float(metrics.get("range_low"))
        range_mid = safe_float(metrics.get("range_mid"))
        range_position = safe_float(metrics.get("range_position"), 0.5)
        confidence = int(safe_float(regime_result.get("confidence")))

        if range_high <= range_low or range_mid <= 0:
            return self._disabled(
                symbol=symbol,
                reason="invalid_range_metrics",
                failed_checks=["range_metrics"],
            )

        five_min = _clean_ohlcv(df_5m, min_len=30)
        fifteen_min = _clean_ohlcv(df_15m, min_len=30)
        if five_min is None or fifteen_min is None:
            return self._disabled(
                symbol=symbol,
                reason="invalid_trigger_data",
                failed_checks=["market_data"],
            )

        candle = _candle_metrics(five_min.iloc[-1])
        entry = candle["close"]
        if entry <= 0:
            return self._disabled(
                symbol=symbol,
                reason="invalid_entry_price",
                failed_checks=["price"],
            )

        prev_volume = safe_float(five_min["volume"].tail(21).iloc[:-1].mean())
        trigger_volume = safe_float(five_min["volume"].iloc[-1])
        volume_ratio = trigger_volume / prev_volume if prev_volume > 0 else 0.0
        atr_price = _average_true_range(fifteen_min, length=14)
        range_width = range_high - range_low
        edge_tolerance = max(range_width * 0.04, entry * 0.0015)
        stop_buffer = max(atr_price * 0.25, entry * (self.min_stop_pct / 100.0))

        if side == "SHORT":
            touched_edge = candle["high"] >= range_high - edge_tolerance
            reclaimed_inside = candle["close"] < range_high
            rejection = candle["close"] <= candle["open"] or candle["upper_wick_ratio"] >= 0.35
            stop_loss = range_high + stop_buffer
            target = range_mid
            edge_score = clamp((range_position - 0.78) / 0.22, 0.0, 1.0)
            direction_ok = target < entry < stop_loss
        else:
            touched_edge = candle["low"] <= range_low + edge_tolerance
            reclaimed_inside = candle["close"] > range_low
            rejection = candle["close"] >= candle["open"] or candle["lower_wick_ratio"] >= 0.35
            stop_loss = range_low - stop_buffer
            target = range_mid
            edge_score = clamp((0.22 - range_position) / 0.22, 0.0, 1.0)
            direction_ok = stop_loss < entry < target

        failed_checks: list[str] = []
        if not touched_edge:
            failed_checks.append("edge_touch")
        if not reclaimed_inside:
            failed_checks.append("reclaim_inside_range")
        if not rejection:
            failed_checks.append("edge_rejection")

        rr = round(_risk_reward_ratio(entry, stop_loss, target), 4)
        if not direction_ok or rr < self.min_rr:
            failed_checks.append("risk_reward")

        volume_ok = 0 < volume_ratio <= self.max_trigger_volume_ratio
        if not volume_ok:
            failed_checks.append("volume_exhaustion")

        rejection_score = 1.0 if rejection and reclaimed_inside and touched_edge else 0.0
        volume_score = clamp((self.max_trigger_volume_ratio - volume_ratio) / self.max_trigger_volume_ratio, 0.0, 1.0)
        rr_score = clamp((rr - self.min_rr) / 2.0, 0.0, 1.0)
        confidence_score = clamp(confidence / 100.0, 0.0, 1.0)

        score = round(
            25.0 * confidence_score
            + 25.0 * edge_score
            + 25.0 * rejection_score
            + 10.0 * volume_score
            + 15.0 * rr_score
        )

        if "risk_reward" in failed_checks:
            status = STATUS_REJECT_RR
            reason = f"rr_below_threshold:{rr}/{self.min_rr}"
        elif "volume_exhaustion" in failed_checks:
            status = STATUS_WAIT_VOLUME
            reason = f"volume_ratio_too_high:{round(volume_ratio, 4)}/{self.max_trigger_volume_ratio}"
        elif failed_checks:
            status = STATUS_WAIT_RECLAIM
            reason = "waiting_for_range_edge_reclaim"
        elif score < self.min_score:
            status = STATUS_WAIT_RECLAIM
            reason = f"score_below_threshold:{score}/{self.min_score}"
            failed_checks.append("score")
        else:
            status = STATUS_WATCH_ONLY
            reason = ""

        return {
            "strategy": STRATEGY_NAME,
            "symbol": symbol,
            "status": status,
            "read_only": True,
            "execution_disabled": True,
            "score": score,
            "threshold": self.min_score,
            "side": side,
            "reason": reason,
            "failed_checks": failed_checks,
            "order_type": "Limit",
            "entry": round(entry, 8),
            "stop_loss": round(stop_loss, 8),
            "target": round(target, 8),
            "rr": rr,
            "range": {
                "high": round(range_high, 8),
                "low": round(range_low, 8),
                "mid": round(range_mid, 8),
                "position": round(range_position, 4),
                "edge_tolerance": round(edge_tolerance, 8),
            },
            "checks": {
                "touched_edge": touched_edge,
                "reclaimed_inside": reclaimed_inside,
                "edge_rejection": rejection,
                "volume_ratio": round(volume_ratio, 4),
                "max_trigger_volume_ratio": self.max_trigger_volume_ratio,
                "min_rr": self.min_rr,
                "atr_price": round(atr_price, 8),
            },
        }

