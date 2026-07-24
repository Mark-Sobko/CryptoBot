import os
import logging
import datetime
from pathlib import Path
from typing import Dict, List, Any, TypedDict, Final
from dotenv import load_dotenv

load_dotenv()

# --- [PROJECT STRUCTURE & PATHS] ---
BASE_DIR: Final[Path] = Path(__file__).parent.absolute()
DATA_DIR: Final[Path] = BASE_DIR / "data"
LOGS_DIR: Final[Path] = BASE_DIR / "logs"

for folder in (DATA_DIR, LOGS_DIR):
    folder.mkdir(parents=True, exist_ok=True)

DB_PATH: Final[str] = str(DATA_DIR / "bot_memory.db")
LOG_PATH: Final[str] = str(LOGS_DIR / "bot_execution.log")
HISTORY_PATH: Final[str] = str(DATA_DIR / "trade_history.json")

# --- [SECURITY & API CONFIGURATION] ---
API_KEY: Final[str] = os.getenv("BYBIT_API_KEY", "")
API_SECRET: Final[str] = os.getenv("BYBIT_API_SECRET", "")

BYBIT_DEMO: Final[bool] = os.getenv("BYBIT_DEMO", "true").lower() == "true"
BYBIT_TESTNET: Final[bool] = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

TELEGRAM_BOT_TOKEN: Final[str] = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID: Final[str] = os.getenv("TELEGRAM_CHAT_ID", "")

if not API_KEY or not API_SECRET:
    raise ValueError("[CRITICAL] BYBIT_API_KEY / BYBIT_API_SECRET missing in .env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    raise ValueError(f"[CONFIG CRITICAL] {name} must be boolean.")


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default

    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"[CONFIG CRITICAL] {name} must be numeric.") from exc


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"[CONFIG CRITICAL] {name} must be integer.") from exc

# --- [SMC & ANALYTICS CONFIG] ---
class SMCConfig(TypedDict):
    lookback_bars: int
    liquidity_lookback: int
    structure_lookback: int
    pd_lookback: int
    impulse_mult: float
    fvg_min_pct: float
    eq_level_threshold: float
    sweep_vol_spike: float
    min_atr_pct: float


SMC_SETTINGS: Final[SMCConfig] = {
    "lookback_bars": 150,
    "liquidity_lookback": 60,
    "structure_lookback": 40,
    "pd_lookback": 80,
    "impulse_mult": 1.2,
    "fvg_min_pct": 0.008,
    "eq_level_threshold": 0.0006,
    "sweep_vol_spike": 1.1,
    "min_atr_pct": 0.02,
}

# --- [SCORING SYSTEM MATRIX] ---
SCORE_WEIGHTS: Final[Dict[str, int]] = {
    "trend": 25,
    "structure": 20,
    "zone": 20,
    "confirmation": 15,
    "macro": 20,
}

SCORE_MODIFIERS: Final[Dict[str, int]] = {
    "sweep_bonus": 15,
    "pd_discount_bonus": 10,
    "pd_premium_bonus": 10,
    "session_confluence": 5,
    "high_vol_penalty": -20,
}

# --- [RISK MANAGEMENT] ---
class RiskConfig(TypedDict):
    risk_per_trade_pct: float
    max_daily_loss_pct: float
    min_score_to_enter: int
    max_open_trades: int
    trading_hours_utc: List[int]


RISK_MANAGEMENT: Final[Dict[str, Any]] = {
    "weekday": {
        "risk_per_trade_pct": 1.0,
        "max_daily_loss_pct": 3.0,
        "min_score_to_enter": 55,
        "max_open_trades": 30,
        "trading_hours_utc": [0, 24],
    },
    "weekend": {
        "risk_per_trade_pct": 0.5,
        "max_daily_loss_pct": 2.0,
        "min_score_to_enter": 70,
        "max_open_trades": 10,
        "trading_hours_utc": [0, 24],
    },
    "global": {
        "allow_live_trading": _env_bool("ALLOW_LIVE_TRADING", False),
        "max_runtime_minutes": _env_float("MAX_RUNTIME_MINUTES", 30.0),
        "max_orders_per_run": _env_int("MAX_ORDERS_PER_RUN", 1),
        "max_orders_per_cycle": _env_int("MAX_ORDERS_PER_CYCLE", 1),
        "max_order_notional_usd": _env_float("MAX_ORDER_NOTIONAL_USD", 25.0),
        "max_drawdown_limit_pct": _env_float("MAX_DRAWDOWN_LIMIT_PCT", 10.0),
        "max_portfolio_heat_pct": _env_float("MAX_PORTFOLIO_HEAT_PCT", 6.0),
        "max_slippage_pct": _env_float("MAX_SLIPPAGE_PCT", 0.2),
        "require_m5_confirmation": _env_bool("REQUIRE_M5_CONFIRMATION", True),
        "require_pd_alignment": _env_bool("REQUIRE_PD_ALIGNMENT", True),
        "require_liquidity_target": _env_bool("REQUIRE_LIQUIDITY_TARGET", True),
        "leverage": 10,
        "margin_type": "ISOLATED",
        "retry_attempts": 3,
        "request_timeout": 30,
    },
}

# --- [EXECUTION SETTINGS] ---
TRADE_EXECUTION: Final[Dict[str, Any]] = {
    "min_rr_allowed": 0.3, # Добавь эту строку
    "tp_ratios": [1.0, 3.0, 5.0],
    "partial_close_pct": 0.5,
    "move_to_be_at_rr": 0.5,
    "trailing_stop_atr_mult": 1.0,
    "slippage_tolerance_pct": 0.005,
}

# --- [TRADABLE ASSETS] ---
SYMBOLS: Final[List[str]] = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT",
    "ARBUSDT", "AVAXUSDT", "LINKUSDT",
    "OPUSDT", "NEARUSDT", "SUIUSDT",
    "TIAUSDT", "XRPUSDT", "INJUSDT",
    "RENDERUSDT",
    # --- [НОВЫЕ ДОБАВЛЕННЫЕ МОНЕТЫ] ---
    # Слои 1/2 с мощными институциональными объемами (Идеально для BOS/CHoCH)
    "APTUSDT",      # Aptos — высокая интрадей-волатильность, четкие POI
    "SEIUSDT",      # Sei — очень технично ходит по тренду
    "DOTUSDT",      # Polkadot — тяжелая ликвидность, долгие направленные тренды
    "POLUSDT",    # Polygon — классика, отлично отрабатывает FVG

    # AI & DePIN секторы (Где сейчас сидят самые агрессивные объемы)
    "TAOUSDT",      # Bittensor — дорогой, но невероятно техничный инструмент для SMC
    "WIFUSDT",      # Лидер среди мем-топов Bybit по суточному обороту (вместо PEPE)

    # Старая гвардия и классика деривативов (Железобетонный ликвид)
    "BNBUSDT",      # Биржа №2 по ликвидности, без резких ложных сквизов
    "LTCUSDT",      # Litecoin — ходит как швейцарские часы, очень чистые структуры
    "BCHUSDT",      # Bitcoin Cash — дает мощные импульсы (идеально для импульсных OB)
]

BLACKLIST: Final[List[str]] = [
    "LUNCUSDT",
    "USTCUSDT",
    "PEPEUSDT",
]

# --- [LOGGING] ---
LOG_LEVEL: Final[int] = logging.DEBUG
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

LOGGING_CONFIG: Final[Dict[str, Any]] = {
    "level": LOG_LEVEL,
    "format": LOG_FORMAT,
    "handlers": [
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
}

# --- [TELEGRAM] ---
TELEGRAM_CONFIG: Final[Dict[str, Any]] = {
    "enabled": True,
    "send_charts": True,
    "alerts": {
        "entry": True,
        "exit": True,
        "error": True,
        "daily_report": True,
    },
}


def get_current_risk() -> Dict[str, Any]:
    day = datetime.datetime.now(datetime.timezone.utc).weekday()
    mode = "weekend" if day >= 5 else "weekday"
    return dict(RISK_MANAGEMENT[mode])


class DynamicGlobalRisk:
    @staticmethod
    def get_config() -> Dict[str, Any]:
        cfg = dict(RISK_MANAGEMENT["global"])
        cfg["max_daily_loss_pct"] = get_current_risk()["max_daily_loss_pct"]
        cfg["risk_per_trade_pct"] = get_current_risk()["risk_per_trade_pct"]
        cfg["max_open_trades"] = get_current_risk()["max_open_trades"]
        return cfg

# =========================================================================
# [INSTITUTIONAL SCALING] Валидация конфигурации перед стартом
# =========================================================================
def _validate_config() -> None:
    """Защита от опечаток (Fat-finger protection)."""
    # 1. Проверка риска
    for mode in ["weekday", "weekend"]:
        risk = RISK_MANAGEMENT[mode].get("risk_per_trade_pct", 0)
        if not (0 < risk <= 20):  # Запрет риска > 20% на сделку
            raise ValueError(f"[CONFIG CRITICAL] Риск {risk}% в режиме {mode} опасен или некорректен!")
            
    # 2. Проверка TP уровней
    tp_ratios = TRADE_EXECUTION.get("tp_ratios", [])
    if not tp_ratios or not isinstance(tp_ratios, list):
        raise ValueError("[CONFIG CRITICAL] tp_ratios должен быть непустым списком.")
        
    # 3. Проверка SMC настроек
    if SMC_SETTINGS.get("lookback_bars", 0) < 50:
        raise ValueError("[CONFIG CRITICAL] SMC_SETTINGS lookback_bars слишком мал для анализа.")

    # 4. Runtime safeguards for main.py
    global_cfg = RISK_MANAGEMENT.get("global", {})
    if not isinstance(global_cfg.get("allow_live_trading", False), bool):
        raise ValueError("[CONFIG CRITICAL] allow_live_trading must be boolean.")

    boolean_flags = [
        "require_m5_confirmation",
        "require_pd_alignment",
        "require_liquidity_target",
    ]
    for key in boolean_flags:
        if not isinstance(global_cfg.get(key, True), bool):
            raise ValueError(f"[CONFIG CRITICAL] {key} must be boolean.")

    non_negative_limits = [
        "max_runtime_minutes",
        "max_orders_per_run",
        "max_orders_per_cycle",
        "max_order_notional_usd",
    ]
    for key in non_negative_limits:
        value = float(global_cfg.get(key, 0))
        if value < 0:
            raise ValueError(f"[CONFIG CRITICAL] {key} must be >= 0.")

    max_slippage_pct = float(global_cfg.get("max_slippage_pct", 0.2))
    if not (0 < max_slippage_pct <= 10):
        raise ValueError("[CONFIG CRITICAL] max_slippage_pct must be in (0, 10].")

    max_portfolio_heat_pct = float(global_cfg.get("max_portfolio_heat_pct", 6.0))
    if not (0 < max_portfolio_heat_pct <= 100):
        raise ValueError("[CONFIG CRITICAL] max_portfolio_heat_pct must be in (0, 100].")

    max_drawdown_limit_pct = float(global_cfg.get("max_drawdown_limit_pct", 10.0))
    if not (0 <= max_drawdown_limit_pct <= 100):
        raise ValueError("[CONFIG CRITICAL] max_drawdown_limit_pct must be in [0, 100].")

# Запускаем проверку при импорте модуля
_validate_config()
