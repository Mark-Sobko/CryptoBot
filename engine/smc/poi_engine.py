import logging
from typing import Dict, Any, Optional, List

import pandas as pd

import config
from engine.smc.smc_utils import validate_ohlcv, calculate_atr, candle_body


class POIEngine:
    """
    OB / FVG / P&D / Mitigation / POI scoring.
    [INSTITUTIONAL EXPANSION]: Добавлена логика Confluence (Слияние OB + FVG).
    """

    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.POIEngine")
        self.settings = getattr(config, "SMC_SETTINGS", {})
        self.pd_lookback = int(self.settings.get("pd_lookback", 250))
        self.fvg_min_pct = float(self.settings.get("fvg_min_pct", 0.04))
        self.impulse_mult = float(self.settings.get("impulse_mult", 1.2))

    def get_pd_zones(self, df: pd.DataFrame) -> Dict[str, Any]:
        if not validate_ohlcv(df, 30):
            return {
                "high": None,
                "low": None,
                "equilibrium": None,
                "current_zone": "UNKNOWN",
            }

        lookback = min(len(df), self.pd_lookback)
        high = float(df["high"].tail(lookback).max())
        low = float(df["low"].tail(lookback).min())
        eq = (high + low) / 2.0
        close = float(df["close"].iloc[-1])

        return {
            "high": high,
            "low": low,
            "equilibrium": eq,
            "current_zone": "PREMIUM" if close > eq else "DISCOUNT",
        }

    def find_poi(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        if not validate_ohlcv(df, 80):
            return None

        pd_zones = self.get_pd_zones(df)

        obs = self._find_order_blocks(df, pd_zones)
        fvgs = self._find_fvgs(df, pd_zones)

        candidates: List[Dict[str, Any]] = []
        candidates.extend(obs)
        candidates.extend(fvgs)

        # =====================================================================
        # [INSTITUTIONAL SCALING] Confluence Zones (Unicorn Setups)
        # Ищем идеальное совпадение: немитигированный OB внутри немитигированного FVG
        # =====================================================================
        confluence_zones = self._find_confluence_zones(obs, fvgs, pd_zones, df)
        candidates.extend(confluence_zones)
        # =====================================================================

        if not candidates:
            return None

        valid = [c for c in candidates if not c.get("mitigated", True)]

        if not valid:
            return None

        valid.sort(key=lambda x: x.get("score", 0), reverse=True)
        # Возвращаем самую сильную зону
        return valid[0]

    def _score_poi(
        self,
        poi: Dict[str, Any],
        df: pd.DataFrame,
        pd_zones: Dict[str, Any],
    ) -> int:
        score = 50

        if poi["type"] == "OB":
            score += 15

        if poi["type"] == "FVG":
            score += 8

        # [INSTITUTIONAL SCALING] Огромный бонус за слияние OB и FVG
        if poi["type"] == "OB+FVG":
            score += 35

        if poi["side"] == "LONG" and poi.get("pd_context") == "DISCOUNT":
            score += 15

        if poi["side"] == "SHORT" and poi.get("pd_context") == "PREMIUM":
            score += 15

        if poi.get("displacement_strength", 0) >= 1.2:
            score += 10

        if poi.get("gap_pct", 0) >= self.fvg_min_pct * 2:
            score += 5

        age = int(poi.get("age", 0))
        if age <= 5:
            score += 5
        elif age > 20:
            score -= 10

        return max(0, min(100, score))

    def _is_zone_mitigated(
        self,
        df: pd.DataFrame,
        start_idx: int,
        side: str,
        top: float,
        bottom: float,
    ) -> bool:
        if start_idx >= len(df) - 1:
            return False

        future = df.iloc[start_idx + 1 :]

        if future.empty:
            return False

        if side == "LONG":
            return bool((future["low"].astype(float) <= bottom).any())

        if side == "SHORT":
            return bool((future["high"].astype(float) >= top).any())

        return True

    def _find_order_blocks(
        self,
        df: pd.DataFrame,
        pd_zones: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []

        try:
            df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

            atr = calculate_atr(df, 14)
            bodies = candle_body(df)

            open_s = df["open"].astype(float)
            close_s = df["close"].astype(float)
            high_s = df["high"].astype(float)
            low_s = df["low"].astype(float)

            start_idx = len(df) - 4
            end_idx = max(2, len(df) - 35)

            for i in range(start_idx, end_idx, -1):
                next_body = float(bodies.iloc[i + 1])
                current_atr = float(atr.iloc[i + 1]) if pd.notna(atr.iloc[i + 1]) else 0.0

                if current_atr <= 0:
                    continue

                displacement_strength = next_body / current_atr

                if displacement_strength < self.impulse_mult:
                    continue

                age = len(df) - i

                # Bullish OB
                if close_s.iloc[i] < open_s.iloc[i] and low_s.iloc[i] < pd_zones["equilibrium"]:
                    if low_s.iloc[i + 2] > high_s.iloc[i]:
                        top = float(high_s.iloc[i])
                        bottom = float(low_s.iloc[i])

                        mitigated = self._is_zone_mitigated(df, i, "LONG", top, bottom)

                        poi = {
                            "type": "OB",
                            "side": "LONG",
                            "top": top,
                            "bottom": bottom,
                            "mid": (top + bottom) / 2,
                            "pd_context": "DISCOUNT",
                            "displacement_strength": round(displacement_strength, 2),
                            "age": age,
                            "mitigated": mitigated,
                        }
                        poi["score"] = self._score_poi(poi, df, pd_zones)
                        result.append(poi)

                # Bearish OB
                if close_s.iloc[i] > open_s.iloc[i] and high_s.iloc[i] > pd_zones["equilibrium"]:
                    if high_s.iloc[i + 2] < low_s.iloc[i]:
                        top = float(high_s.iloc[i])
                        bottom = float(low_s.iloc[i])

                        mitigated = self._is_zone_mitigated(df, i, "SHORT", top, bottom)

                        poi = {
                            "type": "OB",
                            "side": "SHORT",
                            "top": top,
                            "bottom": bottom,
                            "mid": (top + bottom) / 2,
                            "pd_context": "PREMIUM",
                            "displacement_strength": round(displacement_strength, 2),
                            "age": age,
                            "mitigated": mitigated,
                        }
                        poi["score"] = self._score_poi(poi, df, pd_zones)
                        result.append(poi)

        except Exception as e:
            self.logger.error(f"OB search failed: {e}", exc_info=True)

        return result

    def _find_fvgs(
        self,
        df: pd.DataFrame,
        pd_zones: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []

        try:
            df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

            high_s = df["high"].astype(float)
            low_s = df["low"].astype(float)
            close_s = df["close"].astype(float)

            start_idx = len(df) - 2
            end_idx = max(2, len(df) - 35)

            for i in range(start_idx, end_idx, -1):
                age = len(df) - i

                # Bullish FVG
                if low_s.iloc[i] > high_s.iloc[i - 2]:
                    top = float(low_s.iloc[i])
                    bottom = float(high_s.iloc[i - 2])
                    gap_pct = ((top - bottom) / close_s.iloc[i]) * 100

                    if gap_pct >= self.fvg_min_pct and bottom < pd_zones["equilibrium"]:
                        mitigated = self._is_zone_mitigated(df, i, "LONG", top, bottom)

                        poi = {
                            "type": "FVG",
                            "side": "LONG",
                            "top": top,
                            "bottom": bottom,
                            "mid": (top + bottom) / 2,
                            "pd_context": "DISCOUNT",
                            "gap_pct": round(float(gap_pct), 4),
                            "age": age,
                            "mitigated": mitigated,
                        }
                        poi["score"] = self._score_poi(poi, df, pd_zones)
                        result.append(poi)

                # Bearish FVG
                if high_s.iloc[i] < low_s.iloc[i - 2]:
                    top = float(low_s.iloc[i - 2])
                    bottom = float(high_s.iloc[i])
                    gap_pct = ((top - bottom) / close_s.iloc[i]) * 100

                    if gap_pct >= self.fvg_min_pct and top > pd_zones["equilibrium"]:
                        mitigated = self._is_zone_mitigated(df, i, "SHORT", top, bottom)

                        poi = {
                            "type": "FVG",
                            "side": "SHORT",
                            "top": top,
                            "bottom": bottom,
                            "mid": (top + bottom) / 2,
                            "pd_context": "PREMIUM",
                            "gap_pct": round(float(gap_pct), 4),
                            "age": age,
                            "mitigated": mitigated,
                        }
                        poi["score"] = self._score_poi(poi, df, pd_zones)
                        result.append(poi)

        except Exception as e:
            self.logger.error(f"FVG search failed: {e}", exc_info=True)

        return result

    # =========================================================================
    # [INSTITUTIONAL SCALING] Методы поиска слияний (Confluence)
    # =========================================================================
    def _find_confluence_zones(
        self,
        obs: List[Dict[str, Any]],
        fvgs: List[Dict[str, Any]],
        pd_zones: Dict[str, Any],
        df: pd.DataFrame
    ) -> List[Dict[str, Any]]:
        """
        Ищет пересечения (наложения) между валидными OB и FVG. 
        Сужает зону до их математического пересечения.
        """
        confluence_list = []
        for ob in obs:
            if ob.get("mitigated", True):
                continue

            for fvg in fvgs:
                if fvg.get("mitigated", True) or ob["side"] != fvg["side"]:
                    continue

                # Проверка на наложение зон по вертикали (ценовому диапазону)
                if ob["top"] >= fvg["bottom"] and ob["bottom"] <= fvg["top"]:
                    
                    # Ищем зону строгого пересечения (Overlap)
                    intersect_top = min(ob["top"], fvg["top"])
                    intersect_bottom = max(ob["bottom"], fvg["bottom"])

                    if intersect_top > intersect_bottom:
                        poi = {
                            "type": "OB+FVG",
                            "side": ob["side"],
                            "top": intersect_top,
                            "bottom": intersect_bottom,
                            "mid": (intersect_top + intersect_bottom) / 2,
                            "pd_context": ob["pd_context"],
                            "displacement_strength": ob.get("displacement_strength", 0),
                            "gap_pct": fvg.get("gap_pct", 0),
                            "age": min(ob.get("age", 0), fvg.get("age", 0)), # Берем возраст самого свежего элемента
                            "mitigated": False,
                        }
                        poi["score"] = self._score_poi(poi, df, pd_zones)
                        confluence_list.append(poi)

        return confluence_list