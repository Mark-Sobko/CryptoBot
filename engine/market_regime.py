from __future__ import annotations

import math
from typing import Any

import pandas as pd
import pandas_ta as ta


REGIME_TRENDING = "TRENDING"
REGIME_RANGE = "RANGE"
REGIME_LOW_VOL_COMPRESSION = "LOW_VOL_COMPRESSION"
REGIME_CHOP = "CHOP"
REGIME_DATA_ERROR = "DATA_ERROR"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except Exception:
        return default
    return result if math.isfinite(result) else default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _find_column(columns: Any, prefixes: tuple[str, ...]) -> Any | None:
    for column in columns:
        upper = str(column).upper()
        if any(upper.startswith(prefix) for prefix in prefixes):
            return column
    return None


def _clean_ohlcv(df: pd.DataFrame, *, min_len: int) -> pd.DataFrame | None:
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


def percentile_rank(series: pd.Series, value: float) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty or not math.isfinite(value):
        return 0.0
    return round(float((clean <= value).mean()), 4)


def efficiency_ratio(close: pd.Series, *, length: int = 20) -> float:
    clean = pd.to_numeric(close, errors="coerce").dropna()
    if len(clean) <= length:
        return 0.0

    window = clean.tail(length + 1)
    directional_change = abs(float(window.iloc[-1]) - float(window.iloc[0]))
    path = float(window.diff().abs().sum())
    if path <= 0:
        return 0.0
    return round(directional_change / path, 4)


def relative_volume(volume: pd.Series, *, window: int = 50) -> float:
    clean = pd.to_numeric(volume, errors="coerce").dropna()
    if len(clean) < window + 1:
        return 0.0

    baseline = float(clean.iloc[-window - 1 : -1].mean())
    current = float(clean.iloc[-1])
    if baseline <= 0:
        return 0.0
    return round(current / baseline, 4)


def calculate_regime_metrics(
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
) -> dict[str, Any]:
    htf = _clean_ohlcv(df_1h, min_len=140)
    ltf = _clean_ohlcv(df_15m, min_len=90)
    if htf is None:
        return {"ok": False, "reason": "invalid_1h_data"}
    if ltf is None:
        return {"ok": False, "reason": "invalid_15m_data"}

    close = htf["close"]
    high = htf["high"]
    low = htf["low"]
    current_price = safe_float(close.iloc[-1])
    if current_price <= 0:
        return {"ok": False, "reason": "invalid_price"}

    adx_df = ta.adx(high, low, close, length=14)
    adx_col = _find_column(adx_df.columns, ("ADX",)) if adx_df is not None else None
    adx = safe_float(adx_df[adx_col].iloc[-1]) if adx_col is not None else 0.0

    atr = ta.atr(high, low, close, length=14)
    if atr is None or atr.empty:
        atr_pct = 0.0
        atr_percentile = 0.0
    else:
        atr_pct_series = ((atr / close) * 100.0).replace([float("inf"), float("-inf")], pd.NA)
        atr_pct = safe_float(atr_pct_series.iloc[-1])
        atr_percentile = percentile_rank(atr_pct_series.tail(120), atr_pct)

    bb = ta.bbands(close, length=20, std=2)
    bbu_col = _find_column(bb.columns, ("BBU",)) if bb is not None else None
    bbl_col = _find_column(bb.columns, ("BBL",)) if bb is not None else None
    bbm_col = _find_column(bb.columns, ("BBM",)) if bb is not None else None
    if all(column is not None for column in (bbu_col, bbl_col, bbm_col)):
        bb_width_series = (
            ((bb[bbu_col] - bb[bbl_col]) / bb[bbm_col]) * 100.0
        ).replace([float("inf"), float("-inf")], pd.NA)
        bb_width_pct = safe_float(bb_width_series.iloc[-1])
        bb_width_percentile = percentile_rank(bb_width_series.tail(120), bb_width_pct)
    else:
        bb_width_pct = 0.0
        bb_width_percentile = 0.0

    er = efficiency_ratio(close, length=20)
    rel_vol = relative_volume(ltf["volume"], window=50)

    range_window = ltf.tail(72)
    range_high = safe_float(range_window["high"].max())
    range_low = safe_float(range_window["low"].min())
    range_mid = (range_high + range_low) / 2.0 if range_high > range_low else current_price
    range_width = range_high - range_low
    range_width_pct = (range_width / range_mid) * 100.0 if range_mid > 0 else 0.0
    ltf_close = safe_float(ltf["close"].iloc[-1])
    if range_width > 0:
        range_position = clamp((ltf_close - range_low) / range_width, 0.0, 1.0)
        touch_band = max(range_width * 0.12, range_mid * 0.0008)
        upper_touches = int((range_window["high"] >= range_high - touch_band).sum())
        lower_touches = int((range_window["low"] <= range_low + touch_band).sum())
    else:
        range_position = 0.5
        upper_touches = 0
        lower_touches = 0

    return {
        "ok": True,
        "price": round(current_price, 8),
        "adx": round(adx, 4),
        "atr_pct": round(atr_pct, 4),
        "atr_percentile": atr_percentile,
        "bb_width_pct": round(bb_width_pct, 4),
        "bb_width_percentile": bb_width_percentile,
        "efficiency_ratio": er,
        "relative_volume": rel_vol,
        "range_high": round(range_high, 8),
        "range_low": round(range_low, 8),
        "range_mid": round(range_mid, 8),
        "range_width_pct": round(range_width_pct, 4),
        "range_position": round(range_position, 4),
        "upper_touches": upper_touches,
        "lower_touches": lower_touches,
    }


def classify_regime_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    if not metrics.get("ok"):
        return {
            "regime": REGIME_DATA_ERROR,
            "confidence": 0,
            "reason": metrics.get("reason", "invalid_metrics"),
            "trade_posture": "NO_TRADE",
        }

    adx = safe_float(metrics.get("adx"))
    er = safe_float(metrics.get("efficiency_ratio"))
    atr_rank = safe_float(metrics.get("atr_percentile"))
    bbw_rank = safe_float(metrics.get("bb_width_percentile"))
    rel_vol = safe_float(metrics.get("relative_volume"))
    range_width_pct = safe_float(metrics.get("range_width_pct"))
    range_position = safe_float(metrics.get("range_position"), 0.5)
    upper_touches = int(metrics.get("upper_touches", 0) or 0)
    lower_touches = int(metrics.get("lower_touches", 0) or 0)

    compression = (
        atr_rank <= 0.25
        and bbw_rank <= 0.25
        and adx < 20.0
        and rel_vol <= 1.4
    )
    if compression:
        confidence = round(
            clamp((0.25 - max(atr_rank, bbw_rank)) * 180.0 + (20.0 - adx) * 1.5, 0.0, 100.0)
        )
        return {
            "regime": REGIME_LOW_VOL_COMPRESSION,
            "confidence": confidence,
            "reason": "atr_and_bollinger_compressed",
            "trade_posture": "WAIT_BREAKOUT",
        }

    trending = adx >= 22.0 and er >= 0.25 and atr_rank >= 0.20
    if trending:
        confidence = round(
            clamp((adx - 18.0) * 2.2 + er * 80.0 + atr_rank * 20.0, 0.0, 100.0)
        )
        return {
            "regime": REGIME_TRENDING,
            "confidence": confidence,
            "reason": "adx_efficiency_and_volatility_confirmed",
            "trade_posture": "USE_SMC",
        }

    range_structure = (
        1.0 <= range_width_pct <= 9.0
        and upper_touches >= 2
        and lower_touches >= 2
    )
    range_conditions = (
        range_structure
        and adx <= 21.0
        and er <= 0.28
        and rel_vol >= 0.15
    )
    if range_conditions:
        edge_bonus = 15.0 if range_position <= 0.20 or range_position >= 0.80 else 0.0
        confidence = round(
            clamp(
                35.0
                + (21.0 - adx) * 1.5
                + (0.28 - er) * 80.0
                + min(upper_touches + lower_touches, 12)
                + edge_bonus,
                0.0,
                100.0,
            )
        )
        return {
            "regime": REGIME_RANGE,
            "confidence": confidence,
            "reason": "bounded_range_with_repeated_edges",
            "trade_posture": "EDGE_ONLY",
        }

    return {
        "regime": REGIME_CHOP,
        "confidence": round(clamp(100.0 - abs(adx - 16.0) * 3.0 - er * 50.0, 0.0, 100.0)),
        "reason": "no_high_quality_trend_range_or_compression",
        "trade_posture": "NO_TRADE",
    }


def build_regime_setup(metrics: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any] | None:
    regime = classification.get("regime")
    if regime == REGIME_TRENDING:
        return {"status": "SMC_ONLY", "reason": "trend_regime_uses_existing_smc_engine"}

    if regime == REGIME_LOW_VOL_COMPRESSION:
        return {
            "status": "WAIT_BREAKOUT",
            "reason": "compression_requires_breakout_close_volume_and_retest",
            "breakout_above": metrics.get("range_high"),
            "breakout_below": metrics.get("range_low"),
            "confirmation_required": ["range_break", "volume_expansion", "retest"],
        }

    if regime != REGIME_RANGE:
        return None

    position = safe_float(metrics.get("range_position"), 0.5)
    atr_pct = safe_float(metrics.get("atr_pct"))
    width_pct = safe_float(metrics.get("range_width_pct"))
    if width_pct <= 0:
        return None

    if position >= 0.82:
        side = "SHORT"
        entry_zone = "RANGE_HIGH"
        invalidation = metrics.get("range_high")
        target = metrics.get("range_mid")
    elif position <= 0.18:
        side = "LONG"
        entry_zone = "RANGE_LOW"
        invalidation = metrics.get("range_low")
        target = metrics.get("range_mid")
    else:
        return {
            "status": "RANGE_MID_NO_TRADE",
            "reason": "price_not_at_range_edge",
            "range_position": metrics.get("range_position"),
        }

    return {
        "status": "RANGE_EDGE_WATCH",
        "side": side,
        "entry_zone": entry_zone,
        "target": target,
        "invalidation_reference": invalidation,
        "risk_mode": "reduced",
        "max_risk_pct_hint": 0.25,
        "requires": ["edge_rejection", "liquidity_sweep_or_reclaim", "m5_trigger"],
        "atr_pct": atr_pct,
        "range_position": metrics.get("range_position"),
    }


class MarketRegimeClassifier:
    def analyze(self, data: dict[str, Any]) -> dict[str, Any]:
        metrics = calculate_regime_metrics(data.get("1h"), data.get("15m"))
        classification = classify_regime_metrics(metrics)
        setup = build_regime_setup(metrics, classification)
        return {
            "regime": classification["regime"],
            "confidence": classification["confidence"],
            "reason": classification["reason"],
            "trade_posture": classification["trade_posture"],
            "metrics": metrics,
            "setup": setup,
        }
