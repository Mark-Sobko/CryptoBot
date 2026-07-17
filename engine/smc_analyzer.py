import logging
from typing import Dict, Any, Optional

import pandas as pd

import config
from engine.smc.structure_engine import StructureEngine
from engine.smc.poi_engine import POIEngine
from engine.smc.liquidity_engine import LiquidityEngine


class SMCAnalyzer:
    """
    [INSTITUTIONAL SMC FACADE v5.0]
    Объединяет модули Structure, POI и Liquidity.
    Расширен для поддержки вычисления контекста (Premium/Discount) 
    и мульти-таймфреймового (MTF) анализа.
    """
    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.SMCAnalyzer")
        settings = getattr(config, "SMC_SETTINGS", {})

        self.structure_engine = StructureEngine(
            structure_lookback=int(settings.get("structure_lookback", 120))
        )
        self.poi_engine = POIEngine()
        self.liquidity_engine = LiquidityEngine(
            threshold=float(settings.get("eq_level_threshold", 0.0003))
        )

    def detect_structure(self, df: pd.DataFrame) -> Dict[str, Any]:
        return self.structure_engine.detect_structure(df)

    def find_poi(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        return self.poi_engine.find_poi(df)

    def get_pd_zones(self, df: pd.DataFrame) -> Dict[str, Any]:
        return self.poi_engine.get_pd_zones(df)

    def detect_liquidity_pools(self, df: pd.DataFrame) -> Dict[str, bool]:
        return self.liquidity_engine.detect_liquidity_pools(df)

    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        structure = self.detect_structure(df)
        poi = self.find_poi(df)
        pd_zones = self.get_pd_zones(df)
        liquidity = self.detect_liquidity_pools(df)

        # Оригинальная проверка smc_ok
        direction = structure.get("direction")
        smc_ok = bool(
            structure.get("structure_ok")
            and poi
            and direction == poi.get("side")
        )

        # --- [INSTITUTIONAL SCALING] Обогащение контекста для ScoringSystem ---
        is_pd_aligned = False
        has_liquidity_target = False

        if smc_ok and poi and pd_zones:
            poi_price = float(poi.get("price", poi.get("mid", 0.0)) or 0.0)
            eq_price = float(pd_zones.get("equilibrium", 0.0))
            
            # Premium/Discount Alignment: Лонг должен быть в дискаунте (ниже EQ), Шорт - в премиуме (выше EQ)
            if direction == "LONG" and poi_price < eq_price and eq_price > 0:
                is_pd_aligned = True
            elif direction == "SHORT" and poi_price > eq_price and eq_price > 0:
                is_pd_aligned = True

            # Liquidity Target Alignment: Для лонга нужна ликвидность сверху (EQH), для шорта - снизу (EQL)
            if direction == "LONG" and liquidity.get("has_eqh", False):
                has_liquidity_target = True
            elif direction == "SHORT" and liquidity.get("has_eql", False):
                has_liquidity_target = True
        # ----------------------------------------------------------------------

        return {
            "structure": structure,
            "poi": poi,
            "pd_zones": pd_zones,
            "liquidity": liquidity,
            "smc_ok": smc_ok,
            # Добавленные поля для совместимости со ScoringSystem:
            "direction": direction,
            "is_pd_aligned": is_pd_aligned,
            "has_liquidity_target": has_liquidity_target,
            "has_eqh": liquidity.get("has_eqh", False),
            "has_eql": liquidity.get("has_eql", False)
        }

    # --- [INSTITUTIONAL SCALING] Поддержка Multi-Timeframe (MTF) ---
    def analyze_mtf(self, df_htf: pd.DataFrame, df_ltf: pd.DataFrame) -> Dict[str, Any]:
        """
        Фрактальный SMC Анализ:
        Берет макро-структуру с HTF (High Time Frame), 
        но ищет POI и триггеры ликвидности на LTF (Low Time Frame).
        """
        htf_structure = self.detect_structure(df_htf)
        ltf_structure = self.detect_structure(df_ltf)
        
        ltf_poi = self.find_poi(df_ltf)
        htf_pd_zones = self.get_pd_zones(df_htf) # P/D зоны всегда надежнее с HTF
        ltf_liquidity = self.detect_liquidity_pools(df_ltf)

        htf_dir = htf_structure.get("direction")
        ltf_dir = ltf_structure.get("direction")
        
        # Сильный сигнал - когда структура старшего ТФ и младшего ТФ синхронизированы
        mtf_aligned = bool(htf_dir and ltf_dir and htf_dir == ltf_dir)

        smc_ok = bool(
            mtf_aligned
            and htf_structure.get("structure_ok")
            and ltf_poi
            and htf_dir == ltf_poi.get("side")
        )

        is_pd_aligned = False
        has_liquidity_target = False

        if smc_ok and ltf_poi and htf_pd_zones:
            poi_price = float(ltf_poi.get("price", ltf_poi.get("mid", 0.0)) or 0.0)
            eq_price = float(htf_pd_zones.get("equilibrium", 0.0) or 0.0)

            if htf_dir == "LONG" and eq_price > 0 and poi_price < eq_price:
                is_pd_aligned = True
            elif htf_dir == "SHORT" and eq_price > 0 and poi_price > eq_price:
                is_pd_aligned = True

            if htf_dir == "LONG" and ltf_liquidity.get("has_eqh", False):
                has_liquidity_target = True
            elif htf_dir == "SHORT" and ltf_liquidity.get("has_eql", False):
                has_liquidity_target = True

        return {
            "htf_structure": htf_structure,
            "ltf_structure": ltf_structure,
            "poi": ltf_poi,
            "pd_zones": htf_pd_zones,
            "liquidity": ltf_liquidity,
            "mtf_aligned": mtf_aligned,
            "smc_ok": smc_ok,
            "direction": htf_dir,
            "is_pd_aligned": is_pd_aligned,
            "has_liquidity_target": has_liquidity_target,
            "has_eqh": ltf_liquidity.get("has_eqh", False),
            "has_eql": ltf_liquidity.get("has_eql", False),
        }
