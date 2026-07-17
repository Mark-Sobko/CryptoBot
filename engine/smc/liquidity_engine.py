import logging
from typing import Dict, Any, List

import numpy as np
import pandas as pd


class LiquidityEngine:
    """
    EQH / EQL liquidity pools.
    [INSTITUTIONAL EXPANSION]: Добавлено определение свинговых точек (Pivot points)
    для точного нахождения пулов ликвидности и возврата конкретных ценовых уровней.
    """

    def __init__(self, threshold: float = 0.0003):
        self.logger = logging.getLogger("SMC_BOT.LiquidityEngine")
        self.threshold = threshold
        self.pivot_window = 2 # Количество баров слева и справа для формирования свинга

    def detect_liquidity_pools(self, df: pd.DataFrame) -> Dict[str, Any]:
        # Возвращаем Dict[str, Any] вместо Dict[str, bool], чтобы передавать массивы уровней
        if df is None or df.empty or len(df) < 30:
            return {"has_eqh": False, "has_eql": False, "eqh_levels": [], "eql_levels": []}

        try:
            # --- ОРИГИНАЛЬНАЯ ЛОГИКА (сохранена полностью) ---
            highs = pd.to_numeric(df["high"], errors="coerce").tail(20).dropna().values
            lows = pd.to_numeric(df["low"], errors="coerce").tail(20).dropna().values

            has_eqh = self._has_equal_levels(highs)
            has_eql = self._has_equal_levels(lows)
            # -------------------------------------------------

            # =========================================================================
            # [INSTITUTIONAL SCALING] Продвинутая логика поиска пулов по свингам
            # =========================================================================
            # Ищем не просто близкие цены, а именно экстремумы, где скапливаются стопы
            full_highs = pd.to_numeric(df["high"], errors="coerce").dropna().values
            full_lows = pd.to_numeric(df["low"], errors="coerce").dropna().values
            
            swing_highs = self._get_swing_pivots(full_highs, is_high=True)
            swing_lows = self._get_swing_pivots(full_lows, is_high=False)

            eqh_clusters = self._find_level_clusters(swing_highs)
            eql_clusters = self._find_level_clusters(swing_lows)
            
            # Объединяем результаты: если сработал оригинальный алгоритм ИЛИ продвинутый
            final_has_eqh = has_eqh or (len(eqh_clusters) > 0)
            final_has_eql = has_eql or (len(eql_clusters) > 0)
            # =========================================================================

            return {
                "has_eqh": bool(final_has_eqh),
                "has_eql": bool(final_has_eql),
                "eqh_levels": eqh_clusters,  # Точные цены для целей (Take Profit)
                "eql_levels": eql_clusters,  # Точные цены для целей (Take Profit)
            }

        except Exception as e:
            self.logger.debug(f"Liquidity detection failed: {e}")
            return {"has_eqh": False, "has_eql": False, "eqh_levels": [], "eql_levels": []}

    def _has_equal_levels(self, values: np.ndarray) -> bool:
        """ОРИГИНАЛЬНЫЙ МЕТОД СОХРАНЕН БЕЗ ИЗМЕНЕНИЙ"""
        if len(values) < 2:
            return False

        values = np.sort(values)
        avg = float(np.mean(values))

        if avg <= 0:
            return False

        diffs = np.diff(values)
        return bool(np.any((diffs / avg) < self.threshold))

    # =========================================================================
    # ДОПОЛНИТЕЛЬНЫЕ МЕТОДЫ МАСШТАБИРОВАНИЯ
    # =========================================================================
    def _get_swing_pivots(self, values: np.ndarray, is_high: bool) -> np.ndarray:
        """Находит локальные экстремумы (фракталы)."""
        if len(values) < self.pivot_window * 2 + 1:
            return np.array([])
            
        pivots = []
        for i in range(self.pivot_window, len(values) - self.pivot_window):
            window_slice = values[i - self.pivot_window : i + self.pivot_window + 1]
            center = values[i]
            
            if is_high:
                if center == np.max(window_slice):
                    pivots.append(center)
            else:
                if center == np.min(window_slice):
                    pivots.append(center)
                    
        return np.array(pivots)
        
    def _find_level_clusters(self, pivots: np.ndarray) -> List[float]:
        """Группирует свинги в пулы EQH/EQL, если они находятся на одном уровне."""
        if len(pivots) < 2:
            return []
            
        pivots = np.sort(pivots)
        clusters = []
        visited = set()
        
        for i in range(len(pivots)):
            if i in visited:
                continue
                
            current_cluster = [pivots[i]]
            visited.add(i)
            
            for j in range(i + 1, len(pivots)):
                if j in visited:
                    continue
                
                avg = np.mean(current_cluster)
                if avg > 0 and abs(pivots[j] - avg) / avg < self.threshold:
                    current_cluster.append(pivots[j])
                    visited.add(j)
                    
            if len(current_cluster) >= 2:
                clusters.append(float(np.mean(current_cluster)))
                
        return clusters