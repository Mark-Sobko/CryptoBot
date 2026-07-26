from __future__ import annotations

from typing import Any

import pandas as pd

from engine.market_regime import REGIME_DATA_ERROR, REGIME_RANGE, clamp, safe_float
from engine.strategies.common import (
    candle_metrics,
    clean_ohlcv,
    risk_reward_ratio,
    volume_ratio_at,
)


STRATEGY_NAME = "VOLATILITY_EXPANSION"
STATUS_DISABLED = "DISABLED"
STATUS_WAIT_EXPANSION = "WAIT_EXPANSION"
STATUS_WAIT_BREAKOUT = "WAIT_BREAKOUT"
STATUS_WAIT_CONFIRMATION = "WAIT_CONFIRMATION"
STATUS_REJECT_EXTENSION = "REJECT_EXTENSION"
STATUS_REJECT_RR = "REJECT_RR"
STATUS_WATCH_ONLY = "WATCH_ONLY"


def _ema(series: pd.Series, *, length: int) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").ewm(span=length, adjust=False).mean()


def _atr_series(df: pd.DataFrame, *, length: int = 14) -> pd.Series:
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(length).mean()


class VolatilityExpansionStrategy:
    """Read-only impulse detector for fresh volatility expansion."""

    def __init__(
        self,
        *,
        min_score: int = 80,
        min_rr: float = 1.5,
        min_atr_expansion_ratio: float = 1.25,
        min_volume_ratio: float = 1.45,
        min_body_ratio: float = 0.45,
        max_extension_range_pct: float = 0.75,
        max_extension_atr: float = 1.65,
        min_stop_pct: float = 0.25,
        range_lookback: int = 32,
    ) -> None:
        self.min_score = min_score
        self.min_rr = min_rr
        self.min_atr_expansion_ratio = min_atr_expansion_ratio
        self.min_volume_ratio = min_volume_ratio
        self.min_body_ratio = min_body_ratio
        self.max_extension_range_pct = max_extension_range_pct
        self.max_extension_atr = max_extension_atr
        self.min_stop_pct = min_stop_pct
        self.range_lookback = range_lookback

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
        df_1h: pd.DataFrame | None,
        df_15m: pd.DataFrame | None,
        df_5m: pd.DataFrame | None,
    ) -> dict[str, Any]:
        regime = regime_result.get("regime")
        if regime == REGIME_DATA_ERROR:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="data_error_regime",
                failed_checks=["regime"],
            )

        hourly = clean_ohlcv(df_1h, min_len=80)
        fifteen = clean_ohlcv(df_15m, min_len=max(80, self.range_lookback + 30))
        five = clean_ohlcv(df_5m, min_len=45)
        if hourly is None or fifteen is None or five is None:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="invalid_market_data",
                failed_checks=["market_data"],
            )

        atr = _atr_series(fifteen, length=14)
        atr_now = safe_float(atr.iloc[-1])
        atr_baseline = safe_float(atr.iloc[-45:-10].mean())
        atr_expansion_ratio = atr_now / atr_baseline if atr_baseline > 0 else 0.0

        trigger_15m = candle_metrics(fifteen.iloc[-1])
        volume_ratio = volume_ratio_at(fifteen, len(fifteen) - 1, lookback=40)
        prior_range = fifteen.iloc[-self.range_lookback - 1 : -1]
        range_high = safe_float(prior_range["high"].max())
        range_low = safe_float(prior_range["low"].min())
        range_mid = (range_high + range_low) / 2.0 if range_high > range_low else 0.0
        range_width = range_high - range_low

        if atr_now <= 0 or range_width <= 0 or range_mid <= 0:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="invalid_expansion_metrics",
                failed_checks=["atr", "range"],
            )

        side = None
        edge = 0.0
        if trigger_15m["close"] > range_high:
            side = "LONG"
            edge = range_high
        elif trigger_15m["close"] < range_low:
            side = "SHORT"
            edge = range_low

        base_checks: dict[str, Any] = {
            "regime": regime,
            "range_high": round(range_high, 8),
            "range_low": round(range_low, 8),
            "range_width": round(range_width, 8),
            "atr_now": round(atr_now, 8),
            "atr_baseline": round(atr_baseline, 8),
            "atr_expansion_ratio": round(atr_expansion_ratio, 4),
            "min_atr_expansion_ratio": self.min_atr_expansion_ratio,
            "volume_ratio": round(volume_ratio, 4),
            "min_volume_ratio": self.min_volume_ratio,
            "body_ratio": trigger_15m["body_ratio"],
            "min_body_ratio": self.min_body_ratio,
        }

        expansion_failed: list[str] = []
        if atr_expansion_ratio < self.min_atr_expansion_ratio:
            expansion_failed.append("atr_expansion")
        if volume_ratio < self.min_volume_ratio:
            expansion_failed.append("volume_expansion")
        if trigger_15m["body_ratio"] < self.min_body_ratio:
            expansion_failed.append("impulse_body")
        if expansion_failed:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_EXPANSION,
                reason="waiting_for_clean_volatility_expansion",
                failed_checks=expansion_failed,
                checks=base_checks,
            )

        if side is None:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_BREAKOUT,
                reason="waiting_for_local_range_break",
                failed_checks=["range_break"],
                checks=base_checks,
            )

        extension = abs(trigger_15m["close"] - edge) / range_width if range_width > 0 else 0.0
        extension_atr = abs(trigger_15m["close"] - edge) / atr_now if atr_now > 0 else 0.0
        extension_ok = (
            extension <= self.max_extension_range_pct
            and extension_atr <= self.max_extension_atr
        )

        htf_close = hourly["close"]
        htf_ema50 = _ema(htf_close, length=50)
        htf_price = safe_float(htf_close.iloc[-1])
        htf_ema50_now = safe_float(htf_ema50.iloc[-1])
        htf_ema50_prev = safe_float(htf_ema50.iloc[-7])
        htf_slope_pct = (
            ((htf_ema50_now - htf_ema50_prev) / htf_ema50_prev) * 100.0
            if htf_ema50_prev > 0
            else 0.0
        )
        if side == "LONG":
            htf_not_opposed = not (htf_price < htf_ema50_now and htf_slope_pct < -0.05)
        else:
            htf_not_opposed = not (htf_price > htf_ema50_now and htf_slope_pct > 0.05)

        last_5m = candle_metrics(five.iloc[-1])
        five_volume_ratio = volume_ratio_at(five, len(five) - 1, lookback=20)
        held_beyond_edge = last_5m["close"] > edge if side == "LONG" else last_5m["close"] < edge
        continuation_body = last_5m["body_ratio"] >= max(0.25, self.min_body_ratio - 0.15)
        if side == "LONG":
            continuation_direction = last_5m["close"] > last_5m["open"]
            entry = max(edge, (edge + last_5m["close"]) / 2.0)
            stop_loss = edge - max(atr_now * 0.55, entry * (self.min_stop_pct / 100.0))
            target = entry + max(range_width * 0.85, atr_now * 2.20)
            direction_ok = stop_loss < entry < target
        else:
            continuation_direction = last_5m["close"] < last_5m["open"]
            entry = min(edge, (edge + last_5m["close"]) / 2.0)
            stop_loss = edge + max(atr_now * 0.55, entry * (self.min_stop_pct / 100.0))
            target = entry - max(range_width * 0.85, atr_now * 2.20)
            direction_ok = target < entry < stop_loss

        checks = {
            **base_checks,
            "side": side,
            "edge": round(edge, 8),
            "extension_range_pct": round(extension, 4),
            "max_extension_range_pct": self.max_extension_range_pct,
            "extension_atr": round(extension_atr, 4),
            "max_extension_atr": self.max_extension_atr,
            "htf_price": round(htf_price, 8),
            "htf_ema50": round(htf_ema50_now, 8),
            "htf_ema50_slope_pct": round(htf_slope_pct, 4),
            "htf_not_opposed": htf_not_opposed,
            "held_beyond_edge": held_beyond_edge,
            "continuation_direction": continuation_direction,
            "continuation_body": continuation_body,
            "five_volume_ratio": round(five_volume_ratio, 4),
        }

        if not extension_ok:
            return self._result(
                symbol=symbol,
                status=STATUS_REJECT_EXTENSION,
                reason="expansion_already_overextended",
                failed_checks=["extension"],
                side=side,
                checks=checks,
            )

        confirmation_failed: list[str] = []
        if not htf_not_opposed:
            confirmation_failed.append("htf_opposed")
        if not held_beyond_edge:
            confirmation_failed.append("hold_beyond_edge")
        if not continuation_direction:
            confirmation_failed.append("continuation_direction")
        if not continuation_body:
            confirmation_failed.append("continuation_body")
        if five_volume_ratio <= 0:
            confirmation_failed.append("five_volume")

        if confirmation_failed:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_CONFIRMATION,
                reason="waiting_for_continuation_quality",
                failed_checks=confirmation_failed,
                side=side,
                checks=checks,
            )

        rr = risk_reward_ratio(entry, stop_loss, target)
        if not direction_ok or rr < self.min_rr:
            return self._result(
                symbol=symbol,
                status=STATUS_REJECT_RR,
                reason=f"rr_below_threshold:{round(rr, 4)}/{self.min_rr}",
                failed_checks=["risk_reward"],
                side=side,
                entry=entry,
                stop_loss=stop_loss,
                target=target,
                rr=rr,
                checks={**checks, "min_rr": self.min_rr},
            )

        regime_confidence = int(safe_float(regime_result.get("confidence")))
        atr_score = clamp(
            (atr_expansion_ratio - self.min_atr_expansion_ratio) / self.min_atr_expansion_ratio,
            0.0,
            1.0,
        )
        volume_score = clamp((volume_ratio - self.min_volume_ratio) / self.min_volume_ratio, 0.0, 1.0)
        body_score = clamp((trigger_15m["body_ratio"] - self.min_body_ratio) / (1.0 - self.min_body_ratio), 0.0, 1.0)
        extension_score = clamp(1.0 - (extension_atr / self.max_extension_atr), 0.0, 1.0)
        rr_score = clamp((rr - self.min_rr) / 2.0, 0.0, 1.0)
        score = round(
            15.0 * clamp(regime_confidence / 100.0, 0.0, 1.0)
            + 20.0 * atr_score
            + 20.0 * volume_score
            + 15.0 * body_score
            + 10.0 * extension_score
            + 10.0
            + 10.0 * rr_score
        )

        if score < self.min_score:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_CONFIRMATION,
                reason=f"score_below_threshold:{score}/{self.min_score}",
                failed_checks=["score"],
                side=side,
                score=score,
                entry=entry,
                stop_loss=stop_loss,
                target=target,
                rr=rr,
                checks={**checks, "min_rr": self.min_rr},
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
            checks={**checks, "min_rr": self.min_rr},
        )
