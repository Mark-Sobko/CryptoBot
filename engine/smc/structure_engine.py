import logging
from typing import Dict, Any, List, Tuple

import pandas as pd

from engine.smc.smc_utils import validate_ohlcv, calculate_atr, candle_body


class StructureEngine:
    """
    BOS / SWEEP / DISPLACEMENT engine.
    [INSTITUTIONAL EXPANSION]: Добавлена логика CHOCH (Change of Character) 
    и валидация Displacement через всплеск объемов.
    """

    def __init__(self, structure_lookback: int = 120):
        self.logger = logging.getLogger("SMC_BOT.StructureEngine")
        self.structure_lookback = structure_lookback

    @staticmethod
    def _find_swings(df: pd.DataFrame) -> Tuple[List[int], List[int]]:
        highs = df["high"].astype(float).values
        lows = df["low"].astype(float).values

        swing_highs = []
        swing_lows = []

        for i in range(2, len(df) - 2):
            if (
                highs[i] > highs[i - 1]
                and highs[i] > highs[i - 2]
                and highs[i] >= highs[i + 1]
                and highs[i] >= highs[i + 2]
            ):
                swing_highs.append(i)

            if (
                lows[i] < lows[i - 1]
                and lows[i] < lows[i - 2]
                and lows[i] <= lows[i + 1]
                and lows[i] <= lows[i + 2]
            ):
                swing_lows.append(i)

        return swing_highs, swing_lows

    def _get_major_levels(
        self,
        df: pd.DataFrame,
        idx: int,
        swing_highs: List[int],
        swing_lows: List[int],
    ):
        valid_highs = [i for i in swing_highs if i < idx]
        valid_lows = [i for i in swing_lows if i < idx]

        if not valid_highs or not valid_lows:
            return None, None

        lookback_start = max(0, idx - self.structure_lookback)

        recent_highs = [i for i in valid_highs if i >= lookback_start]
        recent_lows = [i for i in valid_lows if i >= lookback_start]

        if not recent_highs or not recent_lows:
            return None, None

        major_high_idx = max(recent_highs, key=lambda i: float(df["high"].iloc[i]))
        major_low_idx = min(recent_lows, key=lambda i: float(df["low"].iloc[i]))

        return (
            float(df["high"].iloc[major_high_idx]),
            float(df["low"].iloc[major_low_idx]),
        )

    # =========================================================================
    # [INSTITUTIONAL SCALING] Определение микро-тренда для классификации CHOCH
    # =========================================================================
    def _evaluate_micro_trend(
        self, 
        df: pd.DataFrame, 
        swing_highs: List[int], 
        swing_lows: List[int], 
        idx: int
    ) -> str:
        """
        Оценивает структуру последних двух свингов (HH/HL или LH/LL).
        Возвращает: "BULLISH", "BEARISH" или "FLAT"
        """
        valid_highs = [i for i in swing_highs if i < idx]
        valid_lows = [i for i in swing_lows if i < idx]

        if len(valid_highs) < 2 or len(valid_lows) < 2:
            return "FLAT"

        h1, h2 = float(df["high"].iloc[valid_highs[-2]]), float(df["high"].iloc[valid_highs[-1]])
        l1, l2 = float(df["low"].iloc[valid_lows[-2]]), float(df["low"].iloc[valid_lows[-1]])

        if h2 > h1 and l2 > l1:
            return "BULLISH"
        elif h2 < h1 and l2 < l1:
            return "BEARISH"
        
        return "FLAT"
    # =========================================================================

    def detect_displacement(self, df: pd.DataFrame, direction: str) -> Dict[str, Any]:
        result = {
            "valid": False,
            "direction": direction,
            "strength": 0.0,
            "volume_confirmed": False, # [INSTITUTIONAL SCALING]
        }

        if not validate_ohlcv(df, 30):
            return result

        try:
            atr = calculate_atr(df, 14)
            bodies = candle_body(df)

            last_body = float(bodies.iloc[-2])
            last_atr = float(atr.iloc[-2])

            if last_atr <= 0:
                return result

            strength = last_body / last_atr

            # =========================================================================
            # [INSTITUTIONAL SCALING] Валидация объема при импульсе
            # =========================================================================
            vol_confirmed = False
            if "volume" in df.columns:
                try:
                    vol_sma = df["volume"].rolling(20).mean()
                    current_vol = float(df["volume"].iloc[-2])
                    avg_vol = float(vol_sma.iloc[-2]) if not pd.isna(vol_sma.iloc[-2]) else 0.0
                    
                    if avg_vol > 0 and current_vol >= (avg_vol * 1.2): # На 20% выше среднего
                        vol_confirmed = True
                except Exception:
                    pass
            # =========================================================================

            result.update(
                {
                    "valid": strength >= 0.9,
                    "strength": round(strength, 2),
                    "volume_confirmed": vol_confirmed,
                }
            )
            return result

        except Exception as e:
            self.logger.debug(f"Displacement detection failed: {e}")
            return result

    def detect_structure(self, df: pd.DataFrame) -> Dict[str, Any]:
        res = {
            "type": None,
            "direction": None,
            "level": None,
            "is_confirmed": False,
            "strength": 1.0,
            "structure_ok": False,
            "trend_ok": True,
            "reason": "not_evaluated",
            "swing_highs_count": 0,
            "swing_lows_count": 0,
            "scan_depth": 0,
            "nearest_major_high": None,
            "nearest_major_low": None,
            "distance_to_major_high_pct": None,
            "distance_to_major_low_pct": None,
            "closest_level_side": None,
            "closest_level_distance_pct": None,
            "is_choch": False, # [INSTITUTIONAL SCALING]
            "market_phase": "FLAT", # [INSTITUTIONAL SCALING]
            "displacement": {
                "valid": False,
                "strength": 0.0,
                "volume_confirmed": False,
            },
        }

        if not validate_ohlcv(df, 80):
            res["reason"] = "invalid_ohlcv"
            return res

        try:
            df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

            swing_highs, swing_lows = self._find_swings(df)
            res["swing_highs_count"] = len(swing_highs)
            res["swing_lows_count"] = len(swing_lows)

            if not swing_highs or not swing_lows:
                res["reason"] = "no_swings"
                return res

            highs = df["high"].astype(float).values
            lows = df["low"].astype(float).values
            closes = df["close"].astype(float).values

            scan_depth = min(12, len(df) - 3)
            res["scan_depth"] = scan_depth
            found_major_levels = False

            for offset in range(1, scan_depth + 1):
                idx = len(df) - offset

                major_high, major_low = self._get_major_levels(
                    df,
                    idx,
                    swing_highs,
                    swing_lows,
                )

                if major_high is None or major_low is None:
                    continue

                found_major_levels = True
                candle_high = highs[idx]
                candle_low = lows[idx]
                candle_close = closes[idx]

                if (
                    res["nearest_major_high"] is None
                    and res["nearest_major_low"] is None
                    and candle_close > 0
                ):
                    distance_to_high = abs(major_high - candle_close) / candle_close * 100
                    distance_to_low = abs(candle_close - major_low) / candle_close * 100
                    if distance_to_high <= distance_to_low:
                        closest_side = "HIGH"
                        closest_distance = distance_to_high
                    else:
                        closest_side = "LOW"
                        closest_distance = distance_to_low
                    res.update(
                        {
                            "nearest_major_high": float(major_high),
                            "nearest_major_low": float(major_low),
                            "distance_to_major_high_pct": round(distance_to_high, 4),
                            "distance_to_major_low_pct": round(distance_to_low, 4),
                            "closest_level_side": closest_side,
                            "closest_level_distance_pct": round(closest_distance, 4),
                        }
                    )

                # Оцениваем макро-тренд перед пробоем
                phase = self._evaluate_micro_trend(df, swing_highs, swing_lows, idx)
                res["market_phase"] = phase

                if candle_high > major_high:
                    if candle_close > major_high:
                        displacement = self.detect_displacement(df.iloc[: idx + 1], "LONG")
                        
                        # [INSTITUTIONAL SCALING] Классификация CHOCH vs BOS
                        struct_type = "CHOCH" if phase == "BEARISH" else "BOS"
                        is_choch = (struct_type == "CHOCH")
                        
                        res.update(
                            {
                                "type": struct_type,
                                "direction": "LONG",
                                "level": float(major_high),
                                "is_confirmed": True,
                                "structure_ok": displacement["valid"],
                                "reason": (
                                    "structure_confirmed"
                                    if displacement["valid"]
                                    else "displacement_not_valid"
                                ),
                                "strength": displacement["strength"],
                                "is_choch": is_choch,
                                "displacement": displacement,
                            }
                        )
                    else:
                        res.update(
                            {
                                "type": "SWEEP",
                                "direction": "SHORT",
                                "level": float(major_high),
                                "is_confirmed": True,
                                "structure_ok": True, # Sweep является самодостаточным паттерном
                                "reason": "sweep_confirmed",
                                "strength": 1.0,
                            }
                        )
                    return res

                if candle_low < major_low:
                    if candle_close < major_low:
                        displacement = self.detect_displacement(df.iloc[: idx + 1], "SHORT")
                        
                        # [INSTITUTIONAL SCALING] Классификация CHOCH vs BOS
                        struct_type = "CHOCH" if phase == "BULLISH" else "BOS"
                        is_choch = (struct_type == "CHOCH")
                        
                        res.update(
                            {
                                "type": struct_type,
                                "direction": "SHORT",
                                "level": float(major_low),
                                "is_confirmed": True,
                                "structure_ok": displacement["valid"],
                                "reason": (
                                    "structure_confirmed"
                                    if displacement["valid"]
                                    else "displacement_not_valid"
                                ),
                                "strength": displacement["strength"],
                                "is_choch": is_choch,
                                "displacement": displacement,
                            }
                        )
                    else:
                        res.update(
                            {
                                "type": "SWEEP",
                                "direction": "LONG",
                                "level": float(major_low),
                                "is_confirmed": True,
                                "structure_ok": True,
                                "reason": "sweep_confirmed",
                                "strength": 1.0,
                            }
                        )
                    return res

            res["reason"] = (
                "no_break_or_sweep" if found_major_levels else "no_recent_major_levels"
            )
            return res

        except Exception as e:
            self.logger.error(f"Structure detection failed: {e}", exc_info=True)
            res["reason"] = "exception"
            return res
