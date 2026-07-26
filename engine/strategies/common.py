from __future__ import annotations

from typing import Any

import pandas as pd

from engine.market_regime import safe_float


def clean_ohlcv(df: pd.DataFrame | None, *, min_len: int) -> pd.DataFrame | None:
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


def average_true_range(df: pd.DataFrame, *, length: int = 14) -> float:
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


def candle_metrics(row: pd.Series) -> dict[str, float]:
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


def risk_reward_ratio(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return abs(target - entry) / risk


def volume_ratio_at(df: pd.DataFrame, position: int, *, lookback: int = 20) -> float:
    if position <= 0 or len(df) <= position:
        return 0.0

    start = max(0, position - lookback)
    baseline = safe_float(df["volume"].iloc[start:position].mean())
    current = safe_float(df["volume"].iloc[position])
    if baseline <= 0:
        return 0.0
    return current / baseline

