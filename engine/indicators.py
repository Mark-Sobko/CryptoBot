import logging
from typing import Literal, Dict, Any

import pandas as pd
import pandas_ta as ta


class ConfirmationModule:
    """
    [INSTITUTIONAL TRIGGER ENGINE v5.0]
    M5 confirmation:
    - volume spike
    - candle body quality
    - RSI velocity
    - ADX micro-trend
    - no look-ahead bias
    """

    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.ConfirmationModule")
        self.MIN_VOLUME_SPIKE = 1.3
        self.MIN_BODY_RATIO = 0.55
        self.MIN_M5_ADX = 18.0
        
        # Стейт для аудита последнего проверенного сигнала
        self.last_metrics: Dict[str, Any] = {}

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        """Возвращает метрики последнего триггера для логов и алертов."""
        return self.last_metrics

    @staticmethod
    def _find_adx_column(adx_df: pd.DataFrame):
        for col in adx_df.columns:
            if str(col).upper().startswith("ADX"):
                return col
        return None

    def check_m5_entry(
        self,
        df_5m: pd.DataFrame,
        side: Literal["LONG", "SHORT"],
    ) -> bool:
        # Сброс метрик перед новой проверкой
        self.last_metrics = {
            "vol_ratio": 0.0,
            "rsi_velocity": 0.0,
            "adx_strength": 0.0,
            "body_ratio": 0.0,
            "is_trigger": False
        }
        
        if df_5m is None or df_5m.empty or len(df_5m) < 35:
            return False

        try:
            side = str(side).upper().strip()
            if side not in ("LONG", "SHORT"):
                return False

            df_calc = df_5m.iloc[:-1].copy()

            for col in ["open", "high", "low", "close", "volume"]:
                df_calc[col] = pd.to_numeric(df_calc[col], errors="coerce")

            df_calc = df_calc.dropna(subset=["open", "high", "low", "close", "volume"])

            if len(df_calc) < 35:
                return False

            signal = df_calc.iloc[-1]

            vol_sma = float(df_calc["volume"].iloc[-21:-1].mean())
            curr_vol = float(signal["volume"])

            if vol_sma <= 0:
                return False

            vol_ratio = curr_vol / vol_sma
            vol_confirm = vol_ratio >= self.MIN_VOLUME_SPIKE

            full_range = float(signal["high"] - signal["low"])
            body_size = abs(float(signal["close"] - signal["open"]))
            body_ratio = body_size / full_range if full_range > 0 else 0.0
            candle_quality = body_ratio >= self.MIN_BODY_RATIO

            rsi = ta.rsi(df_calc["close"], length=14)
            if rsi is None or rsi.empty or len(rsi) < 5:
                return False

            curr_rsi = float(rsi.iloc[-1])
            rsi_velocity = float(rsi.iloc[-1] - rsi.iloc[-3])

            adx_df = ta.adx(
                df_calc["high"],
                df_calc["low"],
                df_calc["close"],
                length=14,
            )

            if adx_df is None or adx_df.empty:
                return False

            col_adx = self._find_adx_column(adx_df)
            if col_adx is None:
                return False

            adx_strength = float(adx_df[col_adx].iloc[-1])
            is_trending = adx_strength >= self.MIN_M5_ADX

            is_green = float(signal["close"]) > float(signal["open"])
            is_red = float(signal["close"]) < float(signal["open"])

            # Сохраняем вычисленные метрики в стейт
            self.last_metrics.update({
                "vol_ratio": round(vol_ratio, 2),
                "rsi_velocity": round(rsi_velocity, 2),
                "adx_strength": round(adx_strength, 2),
                "body_ratio": round(body_ratio, 2)
            })

            if side == "LONG":
                ok = (
                    rsi_velocity >= 1.5
                    and curr_rsi > 47
                    and vol_confirm
                    and candle_quality
                    and is_trending
                    and is_green
                )

                if ok:
                    self.last_metrics["is_trigger"] = True
                    self.logger.info(
                        f"🚀 [M5 TRIGGER] LONG | Vol={vol_ratio:.2f}x | "
                        f"RSI_Vel={rsi_velocity:.2f} | ADX={adx_strength:.1f}"
                    )
                    return True

            if side == "SHORT":
                ok = (
                    rsi_velocity <= -1.5
                    and curr_rsi < 53
                    and vol_confirm
                    and candle_quality
                    and is_trending
                    and is_red
                )

                if ok:
                    self.last_metrics["is_trigger"] = True
                    self.logger.info(
                        f"🩸 [M5 TRIGGER] SHORT | Vol={vol_ratio:.2f}x | "
                        f"RSI_Vel={rsi_velocity:.2f} | ADX={adx_strength:.1f}"
                    )
                    return True

            return False

        except Exception as e:
            self.logger.error(f"ConfirmationModule failed: {e}", exc_info=True)
            return False

    @staticmethod
    def get_exhaustion_risk(df: pd.DataFrame) -> bool:
        if df is None or df.empty or len(df) < 15:
            return False

        try:
            subset = df.iloc[:-1].tail(3).copy()

            for col in ["close", "volume"]:
                subset[col] = pd.to_numeric(subset[col], errors="coerce")

            subset = subset.dropna(subset=["close", "volume"])

            if len(subset) < 3:
                return False

            vols = subset["volume"].values
            closes = subset["close"].values

            bullish_exhaustion = (
                closes[-1] > closes[-2] > closes[-3]
                and vols[-1] < vols[-2] < vols[-3]
            )

            bearish_exhaustion = (
                closes[-1] < closes[-2] < closes[-3]
                and vols[-1] < vols[-2] < vols[-3]
            )

            return bool(bullish_exhaustion or bearish_exhaustion)

        except Exception:
            return False

    @staticmethod
    def calculate_adaptive_stop(df: pd.DataFrame, multiplier: float = 2.0) -> float:
        if df is None or df.empty or len(df) < 20:
            return 0.0

        try:
            df_calc = df.iloc[:-1].copy()

            for col in ["high", "low", "close"]:
                df_calc[col] = pd.to_numeric(df_calc[col], errors="coerce")

            df_calc = df_calc.dropna(subset=["high", "low", "close"])

            if len(df_calc) < 20:
                return 0.0

            atr = ta.atr(
                df_calc["high"],
                df_calc["low"],
                df_calc["close"],
                length=14,
            )

            if atr is None or atr.empty:
                return 0.0

            value = float(atr.iloc[-1] * multiplier)
            return value if value > 0 else 0.0

        except Exception:
            return 0.0

    # =========================================================================
    # SCALING METHODS (Институциональное расширение - SMC Rejections)
    # =========================================================================

    def detect_pinbar_rejection(self, df: pd.DataFrame, side: Literal["LONG", "SHORT"]) -> bool:
        """
        Обнаруживает SMC Pin-Bar (отскок).
        Полезно для поиска сильных следов маркет-мейкера (rejection of liquidity).
        """
        if df is None or df.empty or len(df) < 2:
            return False
            
        try:
            signal = df.iloc[:-1].iloc[-1]
            op, hi, lo, cl = float(signal['open']), float(signal['high']), float(signal['low']), float(signal['close'])
            
            full_range = hi - lo
            if full_range <= 0:
                return False
                
            body_size = abs(cl - op)
            
            if side == "LONG":
                lower_wick = min(op, cl) - lo
                # Длинный нижний фитиль (>= 50% свечи) и небольшое тело
                if lower_wick / full_range >= 0.5 and body_size / full_range <= 0.3:
                    return True
                    
            elif side == "SHORT":
                upper_wick = hi - max(op, cl)
                # Длинный верхний фитиль (>= 50% свечи) и небольшое тело
                if upper_wick / full_range >= 0.5 and body_size / full_range <= 0.3:
                    return True
                    
            return False
        except Exception:
            return False