"""
[INSTITUTIONAL SMC CORE INITIALIZER v5.0]
Внутренний интерфейс подсистемы Smart Money Concepts.
Расширен: добавлена ленивая загрузка (Lazy Loading) для оптимизации памяти и ускорения запуска.
"""

import logging
import importlib

logger = logging.getLogger("SMC_BOT.Engine.SMC")

# Карта модулей для ленивой загрузки. 
# Сюда легко добавлять новые движки (например, OrderBlocks, BreakerBlocks), не перегружая память на старте.
_MODULES = {
    "SMCAnalyzer": "engine.smc.analyzer",
    "StructureEngine": "engine.smc.structure_engine",
    "POIEngine": "engine.smc.poi_engine",
    "LiquidityEngine": "engine.smc.liquidity_engine",
}

def __getattr__(name):
    """
    Ленивый импорт: модули инициализируются только при фактическом обращении к ним.
    """
    if name in _MODULES:
        module_path = _MODULES[name]
        try:
            module = importlib.import_module(module_path)
            return getattr(module, name)
        except (ImportError, AttributeError) as e:
            logger.critical(f"❌ SMC SUB-MODULE LOAD FAILED: {name} from {module_path}", exc_info=True)
            raise
    raise AttributeError(f"module {__name__} has no attribute {name}")

# Явный экспорт для поддержки IDE (автодополнение) и конструкций вида `from engine.smc import *`
__all__ = [
    "SMCAnalyzer",
    "StructureEngine",
    "POIEngine",
    "LiquidityEngine",
]