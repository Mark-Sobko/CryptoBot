from __future__ import annotations

from typing import Any

import pandas as pd

from engine.market_regime import REGIME_TRENDING, clamp, safe_float
from engine.strategies.common import (
    average_true_range,
    candle_metrics,
    clean_ohlcv,
    risk_reward_ratio,
    volume_ratio_at,
)


STRATEGY_NAME = "TREND_PULLBACK"
STATUS_DISABLED = "DISABLED"
STATUS_WAIT_TREND = "WAIT_TREND"
STATUS_WAIT_PULLBACK = "WAIT_PULLBACK"
STATUS_WAIT_CONFIRMATION = "WAIT_CONFIRMATION"
STATUS_REJECT_RR = "REJECT_RR"
STATUS_WATCH_ONLY = "WATCH_ONLY"


def _ema(series: pd.Series, *, length: int) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").ewm(span=length, adjust=False).mean()


def _recent_volume_ratio(df: pd.DataFrame, *, lookback: int = 20) -> float:
    if len(df) < lookback + 1:
        return 0.0
    baseline = safe_float(df["volume"].iloc[-lookback - 1 : -1].mean())
    current = safe_float(df["volume"].iloc[-1])
    if baseline <= 0:
        return 0.0
    return current / baseline


class TrendPullbackStrategy:
    """Read-only trend-continuation pullback detector."""

    def __init__(
        self,
        *,
        min_score: int = 78,
        min_rr: float = 1.4,
        min_stop_pct: float = 0.25,
        min_trend_slope_pct: float = 0.03,
        max_value_distance_atr: float = 3.25,
        max_pullback_volume_ratio: float = 1.35,
        min_trigger_body_ratio: float = 0.30,
        min_trigger_volume_ratio: float = 0.75,
        max_trigger_volume_ratio: float = 2.60,
    ) -> None:
        self.min_score = min_score
        self.min_rr = min_rr
        self.min_stop_pct = min_stop_pct
        self.min_trend_slope_pct = min_trend_slope_pct
        self.max_value_distance_atr = max_value_distance_atr
        self.max_pullback_volume_ratio = max_pullback_volume_ratio
        self.min_trigger_body_ratio = min_trigger_body_ratio
        self.min_trigger_volume_ratio = min_trigger_volume_ratio
        self.max_trigger_volume_ratio = max_trigger_volume_ratio

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
        if regime_result.get("regime") != REGIME_TRENDING:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="not_trending_regime",
                failed_checks=["regime"],
            )

        hourly = clean_ohlcv(df_1h, min_len=110)
        fifteen = clean_ohlcv(df_15m, min_len=80)
        five = clean_ohlcv(df_5m, min_len=45)
        if hourly is None or fifteen is None or five is None:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="invalid_market_data",
                failed_checks=["market_data"],
            )

        htf_close = hourly["close"]
        htf_ema50 = _ema(htf_close, length=50)
        htf_ema100 = _ema(htf_close, length=100)
        htf_price = safe_float(htf_close.iloc[-1])
        htf_ema50_now = safe_float(htf_ema50.iloc[-1])
        htf_ema100_now = safe_float(htf_ema100.iloc[-1])
        htf_ema50_prev = safe_float(htf_ema50.iloc[-7])
        htf_slope_pct = (
            ((htf_ema50_now - htf_ema50_prev) / htf_ema50_prev) * 100.0
            if htf_ema50_prev > 0
            else 0.0
        )

        if (
            htf_price > htf_ema50_now > htf_ema100_now
            and htf_slope_pct >= self.min_trend_slope_pct
        ):
            side = "LONG"
        elif (
            htf_price < htf_ema50_now < htf_ema100_now
            and htf_slope_pct <= -self.min_trend_slope_pct
        ):
            side = "SHORT"
        else:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_TREND,
                reason="htf_trend_not_aligned",
                failed_checks=["htf_trend"],
                checks={
                    "htf_price": round(htf_price, 8),
                    "htf_ema50": round(htf_ema50_now, 8),
                    "htf_ema100": round(htf_ema100_now, 8),
                    "htf_ema50_slope_pct": round(htf_slope_pct, 4),
                    "min_trend_slope_pct": self.min_trend_slope_pct,
                },
            )

        ltf_close = fifteen["close"]
        ltf_ema20 = _ema(ltf_close, length=20)
        ltf_ema50 = _ema(ltf_close, length=50)
        ltf_price = safe_float(ltf_close.iloc[-1])
        ltf_ema20_now = safe_float(ltf_ema20.iloc[-1])
        ltf_ema50_now = safe_float(ltf_ema50.iloc[-1])
        atr_price = average_true_range(fifteen, length=14)
        if atr_price <= 0 or ltf_price <= 0:
            return self._result(
                symbol=symbol,
                status=STATUS_DISABLED,
                reason="invalid_ltf_volatility",
                failed_checks=["atr"],
            )

        recent_pullback = fifteen.tail(12)
        broader_window = fifteen.tail(36)
        pullback_volume_ratio = _recent_volume_ratio(fifteen, lookback=20)
        value_distance_atr = abs(ltf_price - ltf_ema20_now) / atr_price

        if side == "LONG":
            ltf_aligned = ltf_price > ltf_ema50_now and ltf_ema20_now > ltf_ema50_now
            touched_value = bool(recent_pullback["low"].min() <= ltf_ema20_now + atr_price * 0.35)
            reclaimed_value = ltf_price >= ltf_ema20_now
            structure_held = bool(recent_pullback["low"].min() > ltf_ema50_now - atr_price * 0.30)
            swing_target = safe_float(broader_window["high"].max()) + atr_price * 2.00
        else:
            ltf_aligned = ltf_price < ltf_ema50_now and ltf_ema20_now < ltf_ema50_now
            touched_value = bool(recent_pullback["high"].max() >= ltf_ema20_now - atr_price * 0.35)
            reclaimed_value = ltf_price <= ltf_ema20_now
            structure_held = bool(recent_pullback["high"].max() < ltf_ema50_now + atr_price * 0.30)
            swing_target = safe_float(broader_window["low"].min()) - atr_price * 2.00

        pullback_volume_ok = 0 < pullback_volume_ratio <= self.max_pullback_volume_ratio
        value_distance_ok = value_distance_atr <= self.max_value_distance_atr
        pullback_checks = {
            "side": side,
            "htf_price": round(htf_price, 8),
            "htf_ema50": round(htf_ema50_now, 8),
            "htf_ema100": round(htf_ema100_now, 8),
            "htf_ema50_slope_pct": round(htf_slope_pct, 4),
            "ltf_price": round(ltf_price, 8),
            "ltf_ema20": round(ltf_ema20_now, 8),
            "ltf_ema50": round(ltf_ema50_now, 8),
            "atr_price": round(atr_price, 8),
            "value_distance_atr": round(value_distance_atr, 4),
            "max_value_distance_atr": self.max_value_distance_atr,
            "pullback_volume_ratio": round(pullback_volume_ratio, 4),
            "max_pullback_volume_ratio": self.max_pullback_volume_ratio,
            "ltf_aligned": ltf_aligned,
            "touched_value": touched_value,
            "reclaimed_value": reclaimed_value,
            "structure_held": structure_held,
        }

        failed_pullback: list[str] = []
        if not ltf_aligned:
            failed_pullback.append("ltf_alignment")
        if not touched_value:
            failed_pullback.append("value_touch")
        if not reclaimed_value:
            failed_pullback.append("value_reclaim")
        if not structure_held:
            failed_pullback.append("structure_hold")
        if not value_distance_ok:
            failed_pullback.append("not_chasing")
        if not pullback_volume_ok:
            failed_pullback.append("pullback_volume")

        if failed_pullback:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_PULLBACK,
                reason="waiting_for_controlled_trend_pullback",
                failed_checks=failed_pullback,
                side=side,
                checks=pullback_checks,
            )

        trigger = candle_metrics(five.iloc[-1])
        five_ema20_now = safe_float(_ema(five["close"], length=20).iloc[-1])
        trigger_volume_ratio = volume_ratio_at(five, len(five) - 1, lookback=20)
        limit_entry = (ltf_price + ltf_ema20_now) / 2.0
        if side == "LONG":
            trigger_direction = trigger["close"] > trigger["open"] and trigger["close"] > five_ema20_now
            recent_extreme = safe_float(fifteen.tail(5)["low"].min())
            stop_loss = recent_extreme - max(atr_price * 0.35, limit_entry * (self.min_stop_pct / 100.0))
            target = swing_target
            direction_ok = stop_loss < limit_entry < target
        else:
            trigger_direction = trigger["close"] < trigger["open"] and trigger["close"] < five_ema20_now
            recent_extreme = safe_float(fifteen.tail(5)["high"].max())
            stop_loss = recent_extreme + max(atr_price * 0.35, limit_entry * (self.min_stop_pct / 100.0))
            target = swing_target
            direction_ok = target < limit_entry < stop_loss

        body_ok = trigger["body_ratio"] >= self.min_trigger_body_ratio
        trigger_volume_ok = (
            self.min_trigger_volume_ratio
            <= trigger_volume_ratio
            <= self.max_trigger_volume_ratio
        )
        trigger_checks = {
            **pullback_checks,
            "trigger_close": round(trigger["close"], 8),
            "limit_entry": round(limit_entry, 8),
            "trigger_body_ratio": trigger["body_ratio"],
            "min_trigger_body_ratio": self.min_trigger_body_ratio,
            "trigger_volume_ratio": round(trigger_volume_ratio, 4),
            "min_trigger_volume_ratio": self.min_trigger_volume_ratio,
            "max_trigger_volume_ratio": self.max_trigger_volume_ratio,
            "trigger_direction": trigger_direction,
            "five_ema20": round(five_ema20_now, 8),
        }

        failed_confirmation: list[str] = []
        if not trigger_direction:
            failed_confirmation.append("trigger_direction")
        if not body_ok:
            failed_confirmation.append("trigger_body")
        if not trigger_volume_ok:
            failed_confirmation.append("trigger_volume")

        if failed_confirmation:
            return self._result(
                symbol=symbol,
                status=STATUS_WAIT_CONFIRMATION,
                reason="waiting_for_5m_continuation_trigger",
                failed_checks=failed_confirmation,
                side=side,
                checks=trigger_checks,
            )

        rr = risk_reward_ratio(limit_entry, stop_loss, target)
        if not direction_ok or rr < self.min_rr:
            return self._result(
                symbol=symbol,
                status=STATUS_REJECT_RR,
                reason=f"rr_below_threshold:{round(rr, 4)}/{self.min_rr}",
                failed_checks=["risk_reward"],
                side=side,
                entry=limit_entry,
                stop_loss=stop_loss,
                target=target,
                rr=rr,
                checks={**trigger_checks, "min_rr": self.min_rr},
            )

        confidence = int(safe_float(regime_result.get("confidence")))
        slope_score = clamp(abs(htf_slope_pct) / 0.40, 0.0, 1.0)
        value_score = clamp((self.max_value_distance_atr - value_distance_atr) / self.max_value_distance_atr, 0.0, 1.0)
        pullback_volume_score = clamp(
            (self.max_pullback_volume_ratio - pullback_volume_ratio) / self.max_pullback_volume_ratio,
            0.0,
            1.0,
        )
        trigger_score = 1.0 if trigger_direction and body_ok and trigger_volume_ok else 0.0
        rr_score = clamp((rr - self.min_rr) / 2.0, 0.0, 1.0)
        score = round(
            20.0 * clamp(confidence / 100.0, 0.0, 1.0)
            + 20.0 * slope_score
            + 15.0
            + 15.0 * value_score
            + 10.0 * pullback_volume_score
            + 10.0 * trigger_score
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
                entry=limit_entry,
                stop_loss=stop_loss,
                target=target,
                rr=rr,
                checks={**trigger_checks, "min_rr": self.min_rr},
            )

        return self._result(
            symbol=symbol,
            status=STATUS_WATCH_ONLY,
            reason="",
            failed_checks=[],
            side=side,
            score=score,
            order_type="Limit",
            entry=limit_entry,
            stop_loss=stop_loss,
            target=target,
            rr=rr,
            checks={**trigger_checks, "min_rr": self.min_rr},
        )
