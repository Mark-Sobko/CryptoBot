import logging
import math
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

import config


class MarketFilters:
    """
    [INSTITUTIONAL MARKET GUARD v5.0]
    Фильтрация шума, волатильности, ликвидности и макро-контекста.
    """

    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.MarketFilters")

        self.MIN_ADX = 10.0
        self.MIN_ATR_PCT = float(config.SMC_SETTINGS.get("min_atr_pct", 0.05))
        self.MAX_VOLATILITY_SWELL = 3.5
        self.MIN_EFFICIENCY = 0.05
        self.MIN_RELATIVE_VOLUME = 0.2

        self.last_adx = 0.0
        self.last_er = 0.0
        self.last_atr_pct = 0.0
        self.last_rel_vol = 0.0

    def get_metrics_snapshot(self) -> Dict[str, float]:
        """Возвращает срез последних вычисленных метрик для аудита или алертов."""
        return {
            "adx": self.last_adx,
            "er": self.last_er,
            "atr_pct": self.last_atr_pct,
            "rel_vol": self.last_rel_vol
        }

    def is_market_suitable(self, df: pd.DataFrame) -> bool:
        self.last_adx = 0.0
        self.last_er = 0.0
        self.last_atr_pct = 0.0
        self.last_rel_vol = 0.0

        if df is None or df.empty or len(df) < 65:
            return False

        try:
            df_calc = df.iloc[:-1].copy()

            for col in ["high", "low", "close", "volume"]:
                df_calc[col] = pd.to_numeric(df_calc[col], errors="coerce")

            df_calc = df_calc.dropna(subset=["high", "low", "close", "volume"])

            if len(df_calc) < 65:
                return False

            signal_idx = -1
            current_price = float(df_calc["close"].iloc[signal_idx])

            if current_price <= 0:
                return False

            adx_df = ta.adx(
                df_calc["high"],
                df_calc["low"],
                df_calc["close"],
                length=14,
            )

            if adx_df is None or adx_df.empty:
                return False

            adx_cols = [str(c).upper() for c in adx_df.columns]
            col_adx = None

            for original, upper in zip(adx_df.columns, adx_cols):
                if upper.startswith("ADX"):
                    col_adx = original
                    break

            if col_adx is None:
                return False

            adx = float(adx_df[col_adx].iloc[signal_idx])

            atr_series = ta.atr(
                df_calc["high"],
                df_calc["low"],
                df_calc["close"],
                length=14,
            )

            if atr_series is None or atr_series.empty:
                return False

            atr = float(atr_series.iloc[signal_idx])
            atr_pct = (atr / current_price) * 100 if current_price > 0 else 0.0

            available_bars = len(df_calc)
            rolling_window = min(200, available_bars - 15)

            if rolling_window < 14:
                return False

            hist_atr_pct_series = (atr_series / df_calc["close"]) * 100
            hist_atr_pct = float(
                hist_atr_pct_series.rolling(rolling_window).mean().iloc[signal_idx]
            )

            if not np.isfinite(hist_atr_pct) or hist_atr_pct <= 0:
                hist_atr_pct = atr_pct

            change = abs(
                float(df_calc["close"].iloc[signal_idx])
                - float(df_calc["close"].iloc[signal_idx - 10])
            )

            v_slice = df_calc["close"].iloc[signal_idx - 10:]
            volatility_sum = float(v_slice.diff().abs().sum())
            efficiency_ratio = change / volatility_sum if volatility_sum > 0 else 0.0

            vol_window = min(50, available_bars - 5)

            if vol_window < 10:
                return False

            avg_volume = float(
                df_calc["volume"].iloc[signal_idx - vol_window:signal_idx].mean()
            )
            signal_volume = float(df_calc["volume"].iloc[signal_idx])
            rel_volume = signal_volume / avg_volume if avg_volume > 0 else 0.0

            self.last_adx = round(adx, 2)
            self.last_er = round(efficiency_ratio, 4)
            self.last_atr_pct = round(atr_pct, 4)
            self.last_rel_vol = round(rel_volume, 4)

            checks = {
                "is_trending": adx >= self.MIN_ADX,
                "is_volatile": self.MIN_ATR_PCT <= atr_pct <= (
                    hist_atr_pct * self.MAX_VOLATILITY_SWELL
                ),
                "is_efficient": efficiency_ratio >= self.MIN_EFFICIENCY,
                "has_liquidity": rel_volume >= self.MIN_RELATIVE_VOLUME,
            }

            if checks["is_trending"] and checks["has_liquidity"] and (checks["is_efficient"] or checks["is_volatile"]):
                return True

            failed = [k for k, v in checks.items() if not v]

            self.logger.debug(
                f"⚠️ [MARKET GUARD REJECT] {failed} | "
                f"ADX={adx:.1f}/{self.MIN_ADX} | "
                f"ER={efficiency_ratio:.2f}/{self.MIN_EFFICIENCY} | "
                f"ATR={atr_pct:.3f}%/{self.MIN_ATR_PCT}% | "
                f"RelVol={rel_volume:.2f}x"
            )

            return False

        except Exception as e:
            self.logger.error(f"MarketFilters failed: {e}", exc_info=True)
            return False

    @staticmethod
    def check_macro(macro_data: Dict[str, Any], trade_side: str) -> bool:
        if not macro_data:
            return True

        side = str(trade_side).upper()

        btc_trend = float(macro_data.get("BTC_trend", 0.0) or 0.0)

        dxy_raw = macro_data.get("DXY_trend", macro_data.get("dxy_trend", 0.0))

        try:
            dxy_trend = float(dxy_raw)
        except Exception:
            dxy_str = str(dxy_raw).upper()
            dxy_trend = 1.0 if dxy_str == "UP" else -1.0 if dxy_str == "DOWN" else 0.0

        if side in ("LONG", "BUY"):
            if btc_trend < -1.5:
                return False
            if dxy_trend > 1.0:
                return False

        if side in ("SHORT", "SELL"):
            if btc_trend > 1.5:
                return False
            if dxy_trend < -1.0:
                return False

        return True

    @staticmethod
    def calculate_correlation_risk(
        symbol_list: List[str],
        df_map: Dict[str, pd.DataFrame],
    ) -> float:
        if len(symbol_list) < 2 or not df_map:
            return 0.0

        try:
            close_series = {}

            for symbol in symbol_list:
                df = df_map.get(symbol)

                if df is None or df.empty or "close" not in df.columns:
                    continue

                clean = df.iloc[:-1].copy()
                close_series[symbol] = pd.to_numeric(
                    clean["close"],
                    errors="coerce",
                ).tail(30)

            if len(close_series) < 2:
                return 0.0

            combined = pd.DataFrame(close_series).dropna()

            if combined.empty or len(combined) < 10:
                return 0.0

            corr = combined.corr().values
            np.fill_diagonal(corr, np.nan)

            mean_corr = float(np.nanmean(corr))

            return round(mean_corr, 2) if np.isfinite(mean_corr) else 0.0

        except Exception:
            return 0.5