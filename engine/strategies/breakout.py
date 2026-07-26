from __future__ import annotations

from typing import Any

import pandas as pd

from engine.market_regime import REGIME_LOW_VOL_COMPRESSION, clamp, safe_float
from engine.strategies.common import (
    average_true_range,
    candle_metrics,
    clean_ohlcv,
    risk_reward_ratio,
    volume_ratio_at,
)


STRATEGY_NAME = "BREAKOUT"
STATUS_DISABLED = "DISABLED"
STATUS_WAIT_BREAKOUT = "WAIT_BREAKOUT"
STATUS_WAIT_VOLUME = "WAIT_VOLUME"
STATUS_WAIT_RETEST = "WAIT_RETEST"
STATUS_REJECT_RR = "REJECT_RR"
STATUS_WATCH_ONLY = "WATCH_ONLY"


class BreakoutStrategy:
    """Read-only compression breakout detector that requires retest quality."""

    def __init__(
        self,
        *,
        min_score: int = 75,
        min_rr: float = 1.4,
        min_volume_ratio: float = 1.45,
        min_body_ratio: float = 0.45,
        max_extension_range_pct: float = 0.55,
        min_stop_pct: float = 0.20,
        lookback: int = 10,
    ) -> None:
        self.min_score = min_score
        self.min_rr = min_rr
        self.min_volume_ratio = min_volume_ratio
        self.min_body_ratio = min_body_ratio
        self.max_extension_range_pct = max_extension_range_pct
        self.min_stop_pct = min_stop_pct
        self.lookback = lookback

    def _result(
        self,
        *,
        symbol: str,
        status: str,
        reason: str,
        failed_checks: list[str],
        side: str | None = None,
        score: int = 0,
        order_type: str | None = None,
        entry: float | None = None,
        stop_loss: float | None = None,
        target: float | None = None,
        rr: float | None = None,
        checks: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
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
        }
        if order_type:
            result["order_type"] = order_type
        if entry is not None:
            result["entry"] = round(entry, 8)
        if stop_loss is not None:
            result["stop_loss"] = round(stop_loss, 8)
        if target is not None:
            result["target"] = round(target, 8)
        if rr is not None:
            result["rr"] = round(rr, 4)
        if checks:
            result["checks"] = checks
        return result

    def analyze(
        self,
        *,
        symbol: str,
        regime_result: dict[str, Any],
        df_15m: pd.DataFrame | None,
        df_5m: pd.DataFrame | None,
    ) -> dict[str, Any]:
        setup = regime_result.get("setup")
        if regime_result.get("regime") != REGIME_LOW_VOL_COMPRESSION:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="not_compression_regime",
                failed_checks=["regime"],
            )
        if not isinstance(setup, dict) or setup.get("status") != "WAIT_BREAKOUT":
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="invalid_compression_setup",
                failed_checks=["compression_setup"],
            )

        metrics = regime_result.get("metrics") if isinstance(regime_result.get("metrics"), dict) else {}
        range_high = safe_float(setup.get("breakout_above"), safe_float(metrics.get("range_high")))
        range_low = safe_float(setup.get("breakout_below"), safe_float(metrics.get("range_low")))
        range_mid = safe_float(metrics.get("range_mid"), (range_high + range_low) / 2.0)
        confidence = int(safe_float(regime_result.get("confidence")))

        if range_high <= range_low or range_mid <= 0:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="invalid_compression_range",
                failed_checks=["range_metrics"],
            )

        five_min = clean_ohlcv(df_5m, min_len=45)
        fifteen_min = clean_ohlcv(df_15m, min_len=30)
        if five_min is None or fifteen_min is None:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="invalid_trigger_data",
                failed_checks=["market_data"],
            )

        range_width = range_high - range_low
        edge_tolerance = max(range_width * 0.05, range_mid * 0.0015)
        recent_start = max(1, len(five_min) - self.lookback)
        breakout: dict[str, Any] | None = None

        for position in range(recent_start, len(five_min)):
            candle = candle_metrics(five_min.iloc[position])
            previous_close = safe_float(five_min["close"].iloc[position - 1])
            side = None
            edge = 0.0

            if candle["close"] > range_high and previous_close <= range_high + edge_tolerance:
                side = "LONG"
                edge = range_high
            elif candle["close"] < range_low and previous_close >= range_low - edge_tolerance:
                side = "SHORT"
                edge = range_low

            if not side:
                continue

            breakout = {
                "position": position,
                "side": side,
                "edge": edge,
                "candle": candle,
                "volume_ratio": volume_ratio_at(five_min, position, lookback=20),
            }

        last_candle = candle_metrics(five_min.iloc[-1])
        if breakout is None:
            failed_checks = ["range_break"]
            if last_candle["close"] > range_high or last_candle["close"] < range_low:
                failed_checks.append("fresh_break_not_confirmed")
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_BREAKOUT,
                reason="waiting_for_close_outside_compression_range",
                failed_checks=failed_checks,
                checks={
                    "last_close": round(last_candle["close"], 8),
                    "breakout_above": round(range_high, 8),
                    "breakout_below": round(range_low, 8),
                    "min_volume_ratio": self.min_volume_ratio,
                    "min_body_ratio": self.min_body_ratio,
                },
            )

        side = breakout["side"]
        edge = safe_float(breakout["edge"])
        breakout_candle = breakout["candle"]
        breakout_volume_ratio = safe_float(breakout["volume_ratio"])
        body_ok = breakout_candle["body_ratio"] >= self.min_body_ratio
        volume_ok = breakout_volume_ratio >= self.min_volume_ratio
        extension = abs(breakout_candle["close"] - edge) / range_width if range_width > 0 else 0.0
        extension_ok = extension <= self.max_extension_range_pct

        base_checks = {
            "breakout_edge": round(edge, 8),
            "breakout_close": round(breakout_candle["close"], 8),
            "breakout_volume_ratio": round(breakout_volume_ratio, 4),
            "min_volume_ratio": self.min_volume_ratio,
            "breakout_body_ratio": breakout_candle["body_ratio"],
            "min_body_ratio": self.min_body_ratio,
            "extension_range_pct": round(extension, 4),
            "max_extension_range_pct": self.max_extension_range_pct,
        }

        if not volume_ok:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_VOLUME,
                reason=f"breakout_volume_below_threshold:{round(breakout_volume_ratio, 4)}/{self.min_volume_ratio}",
                failed_checks=["volume_expansion"],
                side=side,
                checks=base_checks,
            )
        if not body_ok:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_BREAKOUT,
                reason=f"breakout_body_below_threshold:{breakout_candle['body_ratio']}/{self.min_body_ratio}",
                failed_checks=["impulse_body"],
                side=side,
                checks=base_checks,
            )

        after_breakout = five_min.iloc[int(breakout["position"]) + 1 :]
        if side == "LONG":
            retest_mask = (
                (after_breakout["low"] <= edge + edge_tolerance)
                & (after_breakout["close"] > edge)
            )
            held_beyond_edge = last_candle["close"] > edge
        else:
            retest_mask = (
                (after_breakout["high"] >= edge - edge_tolerance)
                & (after_breakout["close"] < edge)
            )
            held_beyond_edge = last_candle["close"] < edge

        retest_confirmed = bool(not after_breakout.empty and retest_mask.any() and held_beyond_edge)
        checks = {
            **base_checks,
            "retest_confirmed": retest_confirmed,
            "held_beyond_edge": held_beyond_edge,
            "edge_tolerance": round(edge_tolerance, 8),
        }

        if not extension_ok:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_RETEST,
                reason="breakout_overextended_waiting_for_retest",
                failed_checks=["extension"],
                side=side,
                checks=checks,
            )

        if not retest_confirmed:
            failed_checks = ["retest"]
            if not held_beyond_edge:
                failed_checks.append("hold_beyond_edge")
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_RETEST,
                reason="waiting_for_breakout_retest",
                failed_checks=failed_checks,
                side=side,
                checks=checks,
            )

        entry = last_candle["close"]
        atr_price = average_true_range(fifteen_min, length=14)
        stop_buffer = max(atr_price * 0.35, entry * (self.min_stop_pct / 100.0))
        if side == "LONG":
            stop_loss = edge - stop_buffer
            target = entry + range_width * 0.80
            direction_ok = stop_loss < entry < target
        else:
            stop_loss = edge + stop_buffer
            target = entry - range_width * 0.80
            direction_ok = target < entry < stop_loss

        rr = risk_reward_ratio(entry, stop_loss, target)
        if not direction_ok or rr < self.min_rr:
            return self._result(
                symbol=symbol,
                status=STATUS_REJECT_RR,
                reason=f"rr_below_threshold:{round(rr, 4)}/{self.min_rr}",
                failed_checks=["risk_reward"],
                side=side,
                score=0,
                entry=entry,
                stop_loss=stop_loss,
                target=target,
                rr=rr,
                checks={**checks, "min_rr": self.min_rr, "atr_price": round(atr_price, 8)},
            )

        score = round(
            25.0 * clamp(confidence / 100.0, 0.0, 1.0)
            + 25.0 * clamp((breakout_volume_ratio - self.min_volume_ratio) / 1.5, 0.0, 1.0)
            + 15.0 * clamp((breakout_candle["body_ratio"] - self.min_body_ratio) / 0.4, 0.0, 1.0)
            + 20.0
            + 15.0 * clamp((rr - self.min_rr) / 2.0, 0.0, 1.0)
        )

        if score < self.min_score:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_RETEST,
                reason=f"score_below_threshold:{score}/{self.min_score}",
                failed_checks=["score"],
                side=side,
                score=score,
                entry=entry,
                stop_loss=stop_loss,
                target=target,
                rr=rr,
                checks={**checks, "min_rr": self.min_rr, "atr_price": round(atr_price, 8)},
            )

        return self._result(
            symbol=symbol,
            status=STATUS_WATCH_ONLY,
            reason="",
            failed_checks=[],
            side=side,
            score=score,
            order_type="Limit",
            entry=entry,
            stop_loss=stop_loss,
            target=target,
            rr=rr,
            checks={**checks, "min_rr": self.min_rr, "atr_price": round(atr_price, 8)},
        )

