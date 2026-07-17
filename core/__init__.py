"""
[INSTITUTIONAL CORE INITIALIZER v5.0]
Публичный интерфейс пакета core.
Расширен: добавлена ленивая загрузка (Lazy Loading) для оптимизации старта и памяти.
"""

import logging
import importlib

logger = logging.getLogger("SMC_BOT.CoreInit")

# Карта модулей для ленивой загрузки (масштабируемая архитектура)
_MODULES = {
    "ExchangeManager": "core.exchange",
    "TradeExecutor": "core.executor",
    "RiskManager": "core.risk_manager",
    "TradeDatabase": "core.database",
    "TradeLogger": "core.logger",
    "TelegramNotifier": "core.notifier",
    "InstrumentCache": "core.instrument_cache",
    "TPManager": "core.tp_manager",
    "PositionManager": "core.position_manager",
    "DatabaseSync": "core.database_sync",
}

def __getattr__(name):
    """
    Ленивый импорт: модули инициализируются только при фактическом обращении к ним.
    Защищает ядро от перегрузки памяти на старте.
    """
    if name in _MODULES:
        module_path = _MODULES[name]
        try:
            module = importlib.import_module(module_path)
            return getattr(module, name)
        except ImportError as e:
            logger.critical(f"❌ CORE INITIALIZATION FAILED: {name} from {module_path}. Error: {e}")
            logger.critical(
                "Проверьте структуру проекта, sys.path и наличие всех файлов в папке core."
            )
            raise
    raise AttributeError(f"module {__name__} has no attribute {name}")

# Явный экспорт для поддержки IDE (автодополнение)
__all__ = [
    "ExchangeManager",
    "TradeExecutor",
    "RiskManager",
    "TradeDatabase",
    "TradeLogger",
    "TelegramNotifier",
    "InstrumentCache",
    "TPManager",
    "PositionManager",
    "DatabaseSync",
]

logger.info("✅ Core map initialized successfully (Lazy Loading enabled).")