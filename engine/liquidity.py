import logging
from typing import Dict, Any, List, Optional

import numpy as np
import pandas as pd

import config


class LiquidityEngine:
    """
    [INSTITUTIONAL LIQUIDITY HUB v5.0]
    Детектор:
    - EQH / EQL liquidity pools
    - Swing Failure Pattern / Sweep
    - Liquidity voids / FVG
    """

    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.LiquidityEngine")
        self.settings = getattr(config, "SMC_SETTINGS", {})

        self.lookback = int(self.settings.get("liquidity_lookback", 80))
        self.vol_multiplier = float(self.settings.get("sweep_vol_spike", 1.5))
        self.base_threshold = float(self.settings.get("eq_level_threshold", 0.0003))
        self.fvg_min_pct = float(self.settings.get("fvg_min_pct", 0.04))

    def evaluate_liquidity_context(self, analyze_result: Dict[str, Any]) -> str:
        """
        Масштабируемый метод для оценки общего состояния ликвидности.
        Полезно для ScoringSystem или RiskManager для повышения/понижения веса сигнала.
        """
        if not analyze_result.get("poi_ok", False):
            return "NEUTRAL"
            
        has_voids = len(analyze_result.get("voids", [])) > 0
        sweep_active = analyze_result.get("sweep_active", False)
        
        # Если есть и снятие (SFP) и имбаланс (FVG) - это сильнейший контекст
        if sweep_active and has_voids:
            return "HIGH_PROBABILITY"
        elif sweep_active:
            return "SWEEP_DRIVEN"
        elif has_voids:
            return "IMBALANCE_DRIVEN"
            
        return "STANDARD"

    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        if not self._validate_df(df, min_len=max(35, self.lookback // 2)):
            return self._empty_response()

        try:
            clean = self._prepare_df(df)
            if clean is None or clean.empty:
                return self._empty_response()

            atr_pct = self._get_atr_pct(clean)
            dynamic_threshold = max(self.base_threshold, atr_pct * 0.05)

            pools_data = self.detect_liquidity_pools(clean, dynamic_threshold)
            sweep_data = self.check_sweep_pro(clean)
            voids_data = self.find_liquidity_voids(clean)

            sweep_active = bool(sweep_data.get("is_confirmed", False))
            sweep_side: Optional[str] = None

            if sweep_active:
                if sweep_data.get("type") == "BULLISH_SWEEP":
                    sweep_side = "LONG"
                elif sweep_data.get("type") == "BEARISH_SWEEP":
                    sweep_side = "SHORT"

            has_pools = bool(
                pools_data.get("has_eqh", False)
                or pools_data.get("has_eql", False)
            )
            has_voids = len(voids_data) > 0
            poi_confirmed = bool(sweep_active or has_pools or has_voids)

            return {
                "pools": pools_data,
                "sweep": sweep_data,
                "voids": voids_data,
                "has_eqh": pools_data.get("has_eqh", False),
                "has_eql": pools_data.get("has_eql", False),
                "has_ql": pools_data.get("has_eql", False),
                "sweep_active": sweep_active,
                "liquidity_sweep": sweep_active,
                "sweep_side": sweep_side,
                "direction": sweep_side,
                "poi_ok": poi_confirmed,
                "atr_pct": atr_pct,
            }

        except Exception as e:
            self.logger.error(f"LiquidityEngine analyze failed: {e}", exc_info=True)
            return self._empty_response()

    def detect_liquidity_pools(
        self,
        df: pd.DataFrame,
        threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self._validate_df(df, min_len=30):
            return {
                "has_eqh": False,
                "has_eql": False,
                "levels": {"highs": [], "lows": []},
            }

        threshold = float(threshold if threshold is not None else self.base_threshold)

        try:
            clean = self._prepare_df(df)
            if clean is None or clean.empty:
                return {
                    "has_eqh": False,
                    "has_eql": False,
                    "levels": {"highs": [], "lows": []},
                }

            window = clean.tail(self.lookback).reset_index(drop=True)

            highs_arr = window["high"].values
            lows_arr = window["low"].values

            valid_highs: List[float] = []
            valid_lows: List[float] = []

            for i in range(2, len(window) - 2):
                if (
                    highs_arr[i] > highs_arr[i - 1]
                    and highs_arr[i] > highs_arr[i - 2]
                    and highs_arr[i] >= highs_arr[i + 1]
                    and highs_arr[i] >= highs_arr[i + 2]
                ):
                    valid_highs.append(float(highs_arr[i]))

                if (
                    lows_arr[i] < lows_arr[i - 1]
                    and lows_arr[i] < lows_arr[i - 2]
                    and lows_arr[i] <= lows_arr[i + 1]
                    and lows_arr[i] <= lows_arr[i + 2]
                ):
                    valid_lows.append(float(lows_arr[i]))

            eqh_levels = self._cluster_levels(np.array(valid_highs), threshold)
            eql_levels = self._cluster_levels(np.array(valid_lows), threshold)

            return {
                "has_eqh": len(eqh_levels) > 0,
                "has_eql": len(eql_levels) > 0,
                "levels": {
                    "highs": eqh_levels,
                    "lows": eql_levels,
                },
            }

        except Exception as e:
            self.logger.debug(f"detect_liquidity_pools failed: {e}")
            return {
                "has_eqh": False,
                "has_eql": False,
                "levels": {"highs": [], "lows": []},
            }

    def check_sweep_pro(self, df: pd.DataFrame) -> Dict[str, Any]:
        sweep = {
            "type": "NONE",
            "strength": 0.0,
            "is_confirmed": False,
            "level": None,
        }

        if not self._validate_df(df, min_len=30):
            return sweep

        try:
            clean = self._prepare_df(df)
            if clean is None or len(clean) < 30:
                return sweep

            # Используем последний закрытый бар. Если загрузчик уже отдает только закрытые свечи,
            # это всё равно безопаснее, чем работать по потенциально текущей свече.
            signal = clean.iloc[-2]
            context = clean.iloc[-27:-2]

            if context.empty or len(context) < 10:
                return sweep

            prev_max = float(context["high"].max())
            prev_min = float(context["low"].min())

            avg_vol = float(clean["volume"].iloc[-23:-2].mean())
            signal_vol = float(signal["volume"])

            candle_high = float(signal["high"])
            candle_low = float(signal["low"])
            candle_close = float(signal["close"])

            candle_range = candle_high - candle_low
            if candle_range <= 0:
                return sweep

            vol_spike = signal_vol / avg_vol if avg_vol > 0 else 1.0

            if candle_low < prev_min and candle_close > prev_min:
                rejection = (candle_close - candle_low) / candle_range

                if rejection > 0.60 and vol_spike >= self.vol_multiplier:
                    sweep.update(
                        {
                            "type": "BULLISH_SWEEP",
                            "strength": round(vol_spike, 2),
                            "is_confirmed": True,
                            "level": prev_min,
                        }
                    )

            elif candle_high > prev_max and candle_close < prev_max:
                rejection = (candle_high - candle_close) / candle_range

                if rejection > 0.60 and vol_spike >= self.vol_multiplier:
                    sweep.update(
                        {
                            "type": "BEARISH_SWEEP",
                            "strength": round(vol_spike, 2),
                            "is_confirmed": True,
                            "level": prev_max,
                        }
                    )

            return sweep

        except Exception as e:
            self.logger.debug(f"check_sweep_pro failed: {e}")
            return sweep

    def find_liquidity_voids(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        if not self._validate_df(df, min_len=20):
            return []

        try:
            clean = self._prepare_df(df)
            if clean is None or len(clean) < 3:
                return []

            voids: List[Dict[str, Any]] = []
            subset = clean.iloc[-15:].reset_index(drop=True)

            for i in range(2, len(subset)):
                close_base = float(subset["close"].iloc[i])
                if close_base <= 0:
                    continue

                # Bullish FVG
                if subset["low"].iloc[i] > subset["high"].iloc[i - 2]:
                    top = float(subset["low"].iloc[i])
                    bottom = float(subset["high"].iloc[i - 2])
                    imbalance_pct = (top - bottom) / close_base * 100

                    if imbalance_pct >= self.fvg_min_pct:
                        voids.append(
                            {
                                "type": "FVG",
                                "direction": "UP",
                                "side": "LONG",
                                "top": top,
                                "bottom": bottom,
                                "mid": (top + bottom) / 2,
                                "imbalance_pct": round(imbalance_pct, 3),
                            }
                        )

                # Bearish FVG
                elif subset["high"].iloc[i] < subset["low"].iloc[i - 2]:
                    top = float(subset["low"].iloc[i - 2])
                    bottom = float(subset["high"].iloc[i])
                    imbalance_pct = (top - bottom) / close_base * 100

                    if imbalance_pct >= self.fvg_min_pct:
                        voids.append(
                            {
                                "type": "FVG",
                                "direction": "DOWN",
                                "side": "SHORT",
                                "top": top,
                                "bottom": bottom,
                                "mid": (top + bottom) / 2,
                                "imbalance_pct": round(imbalance_pct, 3),
                            }
                        )

            voids.sort(key=lambda x: x.get("imbalance_pct", 0), reverse=True)
            return voids

        except Exception as e:
            self.logger.debug(f"find_liquidity_voids failed: {e}")
            return []

    def _cluster_levels(self, levels: np.ndarray, threshold: float) -> List[float]:
        if levels is None or len(levels) == 0:
            return []

        levels = np.array([float(x) for x in levels if np.isfinite(float(x))])

        if len(levels) < 2:
            return []

        levels = np.sort(levels)

        clusters: List[float] = []
        current_cluster = [levels[0]]

        for level in levels[1:]:
            base = float(np.mean(current_cluster))
            if base <= 0:
                current_cluster = [level]
                continue

            if abs(level - base) / abs(base) <= threshold:
                current_cluster.append(level)
            else:
                if len(current_cluster) >= 2:
                    clusters.append(float(np.mean(current_cluster)))
                current_cluster = [level]

        if len(current_cluster) >= 2:
            clusters.append(float(np.mean(current_cluster)))

        return clusters

    def _get_atr_pct(self, df: pd.DataFrame) -> float:
        if not self._validate_df(df, min_len=20):
            return 0.001

        try:
            high = df["high"]
            low = df["low"]
            close = df["close"]
            prev_close = close.shift(1)

            tr = pd.concat(
                [
                    high - low,
                    (high - prev_close).abs(),
                    (low - prev_close).abs(),
                ],
                axis=1,
            ).max(axis=1)

            atr = float(tr.rolling(14).mean().iloc[-2])
            price = float(close.iloc[-2])

            if atr <= 0 or price <= 0:
                return 0.001

            return float(atr / price)

        except Exception:
            return 0.001

    @staticmethod
    def _validate_df(df: pd.DataFrame, min_len: int = 30) -> bool:
        if df is None or df.empty or len(df) < min_len:
            return False

        required = {"open", "high", "low", "close", "volume"}
        return required.issubset(df.columns)

    @staticmethod
    def _prepare_df(df: pd.DataFrame) -> Optional[pd.DataFrame]:
        if df is None or df.empty:
            return None

        clean = df.copy()

        for col in ["open", "high", "low", "close", "volume"]:
            clean[col] = pd.to_numeric(clean[col], errors="coerce")

        clean = clean.dropna(subset=["open", "high", "low", "close", "volume"])

        return clean.reset_index(drop=True)

    @staticmethod
    def _empty_response() -> Dict[str, Any]:
        return {
            "pools": {
                "has_eqh": False,
                "has_eql": False,
                "levels": {"highs": [], "lows": []},
            },
            "sweep": {
                "type": "NONE",
                "strength": 0.0,
                "is_confirmed": False,
                "level": None,
            },
            "voids": [],
            "has_eqh": False,
            "has_eql": False,
            "has_ql": False,
            "sweep_active": False,
            "liquidity_sweep": False,
            "sweep_side": None,
            "direction": None,
            "poi_ok": False,
            "atr_pct": 0.001,
        }