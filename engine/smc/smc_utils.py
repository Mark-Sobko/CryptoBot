import math
from typing import Optional

import pandas as pd


def to_float_series(df: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(df[column], errors="coerce")


def is_valid_number(value) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except Exception:
        return False


def calculate_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = to_float_series(df, "high")
    low = to_float_series(df, "low")
    close = to_float_series(df, "close")

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.rolling(length).mean()


def candle_body(df: pd.DataFrame) -> pd.Series:
    return (to_float_series(df, "close") - to_float_series(df, "open")).abs()


def validate_ohlcv(df: pd.DataFrame, min_len: int = 50) -> bool:
    if df is None or df.empty or len(df) < min_len:
        return False

    required = {"open", "high", "low", "close"}
    return required.issubset(df.columns)


# =========================================================================
# [INSTITUTIONAL SCALING] Расширенные утилиты для анализа свечей и объемов
# =========================================================================

def candle_upper_wick(df: pd.DataFrame) -> pd.Series:
    """Возвращает размер верхней тени свечи."""
    high = to_float_series(df, "high")
    open_s = to_float_series(df, "open")
    close_s = to_float_series(df, "close")
    # Максимум минус наибольшее из (Open, Close)
    return high - pd.concat([open_s, close_s], axis=1).max(axis=1)


def candle_lower_wick(df: pd.DataFrame) -> pd.Series:
    """Возвращает размер нижней тени свечи."""
    low = to_float_series(df, "low")
    open_s = to_float_series(df, "open")
    close_s = to_float_series(df, "close")
    # Наименьшее из (Open, Close) минус минимум
    return pd.concat([open_s, close_s], axis=1).min(axis=1) - low


def calculate_rvol(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """
    Relative Volume (RVOL) = Volume / SMA(Volume, length).
    Индикатор институционального следа. Значения > 1.5 говорят о всплеске интереса.
    """
    if "volume" not in df.columns:
        return pd.Series([0.0] * len(df), index=df.index)
    
    vol = to_float_series(df, "volume")
    vol_sma = vol.rolling(length).mean()
    
    # Защита от деления на ноль в неликвидных активах
    rvol = vol / vol_sma.replace(0, pd.NA)
    return rvol.fillna(1.0)