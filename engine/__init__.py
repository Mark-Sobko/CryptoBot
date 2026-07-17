"""
[ENGINE INITIALIZER v5.0]
Публичный интерфейс аналитического ядра engine.
Расширен: добавлена поддержка ленивой инициализации для масштабирования.
"""

import logging
import importlib

logger = logging.getLogger("SMC_BOT.Engine")

__version__ = "5.0.0"
__author__ = "SMC Institutional Team"

# Список модулей для ленивого доступа
_MODULES = {
    "MarketFilters": "engine.filters",
    "ConfirmationModule": "engine.indicators",
    "LiquidityEngine": "engine.liquidity",
    "ScoringSystem": "engine.scoring",
    "SMCAnalyzer": "engine.smc_analyzer",
    "StatsAnalyzer": "engine.stats_analyzer",
    "TrendEngine": "engine.trend_engine",
    "StructureEngine": "engine.smc",
    "POIEngine": "engine.smc",
    "SMCLiquidityEngine": "engine.smc",
}

def __getattr__(name):
    """
    Ленивый импорт: модули загружаются только в момент первого обращения.
    Это критично для масштабирования, если число модулей вырастет до 50+.
    """
    if name in _MODULES:
        module_path = _MODULES[name]
        try:
            module = importlib.import_module(module_path)
            return getattr(module, name)
        except (ImportError, AttributeError) as e:
            logger.critical(f"❌ ENGINE MODULE LOAD FAILED: {name} from {module_path}", exc_info=True)
            raise
    raise AttributeError(f"module {__name__} has no attribute {name}")

# Для обратной совместимости и явного экспорта (IDE будут видеть эти атрибуты)
__all__ = list(_MODULES.keys())

# Лог инициализации ядра
logger.info(f"✅ ENGINE CORE v{__version__} initialized with Lazy Loading support.")