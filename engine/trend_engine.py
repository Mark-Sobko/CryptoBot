import logging
import math
from typing import Literal, Optional, Dict, Any

import pandas as pd
import pandas_ta as ta


TrendDirection = Literal["LONG", "SHORT", "FLAT"]


class TrendEngine:
    """
    [INSTITUTIONAL TREND ENGINE v5.0]
    HTF trend + LTF momentum + volatility phase detector.
    Расширен: MTF alignment, Overextension risk detector.
    """

    logger = logging.getLogger("SMC_BOT.TrendEngine")

    @staticmethod
    def _valid_number(value) -> bool:
        try:
            return value is not None and math.isfinite(float(value))
        except Exception:
            return False

    @staticmethod
    def _find_column(columns, keywords) -> Optional[str]:
        upper_cols = [str(c).upper() for c in columns]

        for original, upper in zip(columns, upper_cols):
            if any(k in upper for k in keywords):
                return original

        return None

    @classmethod
    def get_direction(
        cls,
        df_1h: pd.DataFrame,
        df_15m: pd.DataFrame,
    ) -> TrendDirection:
        if df_1h is None or df_15m is None or df_1h.empty or df_15m.empty:
            return "FLAT"

        if len(df_1h) < 120 or len(df_15m) < 30:
            return "FLAT"

        try:
            required_cols = {"high", "low", "close"}

            if not required_cols.issubset(df_15m.columns) or "close" not in df_1h.columns:
                return "FLAT"

            close_1h = pd.to_numeric(df_1h["close"], errors="coerce").dropna()
            if len(close_1h) < 120:
                return "FLAT"

            ema_fast = ta.ema(close_1h, length=24)
            ema_slow = ta.ema(close_1h, length=120)

            if ema_fast is None or ema_slow is None or ema_fast.empty or ema_slow.empty:
                return "FLAT"

            last_close = close_1h.iloc[-1]
            last_ema_fast = ema_fast.iloc[-1]
            last_ema_slow = ema_slow.iloc[-1]

            if not all(
                cls._valid_number(x)
                for x in (last_close, last_ema_fast, last_ema_slow)
            ):
                return "FLAT"

            bullish_htf = last_close > last_ema_fast > last_ema_slow
            bearish_htf = last_close < last_ema_fast < last_ema_slow

            high_15m = pd.to_numeric(df_15m["high"], errors="coerce")
            low_15m = pd.to_numeric(df_15m["low"], errors="coerce")
            close_15m = pd.to_numeric(df_15m["close"], errors="coerce")

            clean_15m = pd.DataFrame(
                {
                    "high": high_15m,
                    "low": low_15m,
                    "close": close_15m,
                }
            ).dropna()

            if len(clean_15m) < 30:
                return "FLAT"

            adx_data = ta.adx(
                clean_15m["high"],
                clean_15m["low"],
                clean_15m["close"],
                length=14,
            )

            if adx_data is None or adx_data.empty:
                return "FLAT"

            col_adx = cls._find_column(adx_data.columns, ["ADX"])
            col_dmp = cls._find_column(adx_data.columns, ["DMP", "PLUS", "+DI"])
            col_dmn = cls._find_column(adx_data.columns, ["DMN", "MINUS", "-DI"])

            if not all([col_adx, col_dmp, col_dmn]):
                cls.logger.warning(
                    f"⚠️ ADX columns not recognized: {list(adx_data.columns)}"
                )
                return "FLAT"

            adx = adx_data[col_adx].iloc[-1]
            dmp = adx_data[col_dmp].iloc[-1]
            dmn = adx_data[col_dmn].iloc[-1]

            if not all(cls._valid_number(x) for x in (adx, dmp, dmn)):
                return "FLAT"

            strong_momentum = float(adx) > 22.0

            if bullish_htf and float(dmp) > float(dmn) and strong_momentum:
                return "LONG"

            if bearish_htf and float(dmn) > float(dmp) and strong_momentum:
                return "SHORT"

            return "FLAT"

        except Exception as e:
            cls.logger.error(f"TrendEngine calculation failed: {e}", exc_info=True)
            return "FLAT"

    @classmethod
    def get_volatility_status(cls, df: pd.DataFrame) -> str:
        """
        Возвращает:
        - SQUEEZE
        - EXPANSION
        - NORMAL
        """

        if df is None or df.empty or len(df) < 200:
            return "NORMAL"

        try:
            if "close" not in df.columns:
                return "NORMAL"

            close = pd.to_numeric(df["close"], errors="coerce").dropna()

            if len(close) < 200:
                return "NORMAL"

            bbands = ta.bbands(close, length=20, std=2)

            if bbands is None or bbands.empty:
                return "NORMAL"

            col_upper = cls._find_column(bbands.columns, ["BBU"])
            col_lower = cls._find_column(bbands.columns, ["BBL"])
            col_middle = cls._find_column(bbands.columns, ["BBM"])

            if not all([col_upper, col_lower, col_middle]):
                cls.logger.warning(
                    f"⚠️ Bollinger columns not recognized: {list(bbands.columns)}"
                )
                return "NORMAL"

            upper = pd.to_numeric(bbands[col_upper], errors="coerce")
            lower = pd.to_numeric(bbands[col_lower], errors="coerce")
            middle = pd.to_numeric(bbands[col_middle], errors="coerce")

            bbw = ((upper - lower) / middle).replace([float("inf"), float("-inf")], pd.NA)
            bbw = bbw.dropna()

            if len(bbw) < 200:
                return "NORMAL"

            rolling = bbw.rolling(window=200)

            low_threshold = rolling.quantile(0.20).iloc[-1]
            high_threshold = rolling.quantile(0.80).iloc[-1]
            current_bbw = bbw.iloc[-1]

            if not all(
                cls._valid_number(x)
                for x in (low_threshold, high_threshold, current_bbw)
            ):
                return "NORMAL"

            if current_bbw <= low_threshold:
                return "SQUEEZE"

            if current_bbw >= high_threshold:
                return "EXPANSION"

            return "NORMAL"

        except Exception as e:
            cls.logger.debug(f"Volatility status calculation failed: {e}")
            return "NORMAL"

    # =========================================================================
    # [INSTITUTIONAL SCALING] Расширенный анализ тренда (Exhaustion & MTF)
    # =========================================================================

    @classmethod
    def is_trend_overextended(cls, df_htf: pd.DataFrame, threshold_pct: float = 3.5) -> bool:
        """
        Определяет Mean Reversion Risk. 
        Если цена ушла от EMA 120 более чем на threshold_pct, тренд истощен.
        """
        if df_htf is None or df_htf.empty or len(df_htf) < 120 or "close" not in df_htf.columns:
            return False

        try:
            close_htf = pd.to_numeric(df_htf["close"], errors="coerce").dropna()
            ema_slow = ta.ema(close_htf, length=120)

            if ema_slow is None or ema_slow.empty:
                return False

            last_close = float(close_htf.iloc[-1])
            last_ema = float(ema_slow.iloc[-1])

            if last_ema <= 0 or last_close <= 0:
                return False

            distance_pct = abs(last_close - last_ema) / last_ema * 100

            if distance_pct >= threshold_pct:
                cls.logger.warning(
                    f"⚠️ [TREND EXHAUSTION] Price is overextended: {distance_pct:.2f}% away from EMA120."
                )
                return True

            return False
        except Exception:
            return False

    @classmethod
    def get_mtf_alignment(
        cls, 
        df_4h: pd.DataFrame, 
        df_1h: pd.DataFrame, 
        df_15m: pd.DataFrame
    ) -> Dict[str, Any]:
        """
        Сводная оценка выравнивания 3-х таймфреймов для ScoringSystem.
        """
        result = {
            "mtf_aligned": False,
            "strong_trend": False,
            "direction": "FLAT"
        }

        # 1. Получаем базовое направление (1h + 15m)
        base_dir = cls.get_direction(df_1h, df_15m)
        if base_dir == "FLAT":
            return result

        # 2. Сверяем с 4H
        if df_4h is not None and not df_4h.empty and len(df_4h) >= 24:
            try:
                close_4h = pd.to_numeric(df_4h["close"], errors="coerce").dropna()
                ema_4h = ta.ema(close_4h, length=24) # Недельный тренд на 4H
                
                if ema_4h is not None and not ema_4h.empty:
                    last_c4 = float(close_4h.iloc[-1])
                    last_e4 = float(ema_4h.iloc[-1])
                    
                    is_4h_long = last_c4 > last_e4
                    is_4h_short = last_c4 < last_e4
                    
                    if (base_dir == "LONG" and is_4h_long) or (base_dir == "SHORT" and is_4h_short):
                        result["mtf_aligned"] = True
                        result["strong_trend"] = True
            except Exception:
                pass

        result["direction"] = base_dir
        return result