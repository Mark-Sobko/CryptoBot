import datetime
import logging
import math
from decimal import Decimal, ROUND_DOWN
from typing import List, Dict, Any, Tuple, Optional

import config


class RiskManager:
    """
    Institutional Risk Engine.
    Контроль дневной просадки, лимитов позиций, концентрации риска,
    адаптивный расчет размера позиции и защита от некорректных ордеров.
    [INSTITUTIONAL SCALING]: Добавлен контроль общей тепловой нагрузки (Portfolio Heat).
    """

    MIN_POSITION_VALUE_USD = 10.0
    MIN_STOP_PCT = 0.35
    MARGIN_BUFFER = 0.92
    DEFAULT_MIN_RR = 1.0

    def __init__(self, balance: float):
        if balance <= 0 or not math.isfinite(balance):
            raise ValueError("Balance must be positive and finite")

        self.balance = float(balance)
        self.logger = logging.getLogger("RiskManager")

    def get_current_mode(self) -> str:
        now = datetime.datetime.now(datetime.timezone.utc)
        return "weekend" if now.weekday() >= 5 else "weekday"

    def _get_risk_settings(self) -> Dict[str, Any]:
        mode = self.get_current_mode()
        return dict(config.RISK_MANAGEMENT.get(mode, config.RISK_MANAGEMENT["weekday"]))

    def _get_global_settings(self) -> Dict[str, Any]:
        return dict(config.RISK_MANAGEMENT.get("global", {}))

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return symbol.replace("/", "").replace("-", "").upper().strip()

    @staticmethod
    def _round_down(value: float, precision: int = 4) -> float:
        quant = Decimal("1." + "0" * precision)
        return float(Decimal(str(value)).quantize(quant, rounding=ROUND_DOWN))

    # =========================================================================
    # [INSTITUTIONAL SCALING] Контроль общей нагрузки на депозит (Portfolio Heat)
    # =========================================================================
    def _calculate_portfolio_heat(self, active_positions: List[Dict[str, Any]]) -> float:
        """Считает текущий суммарный риск в USD по всем открытым сделкам (Portfolio Heat)"""
        total_risk_usd = 0.0
        for pos in active_positions:
            try:
                entry = float(pos.get("entry_price", pos.get("entryPrice", 0)))
                sl = float(pos.get("stop_loss", pos.get("stopLoss", 0)))
                size = float(pos.get("size", 0))
                
                if entry > 0 and sl > 0 and size > 0:
                    trade_risk_usd = abs(entry - sl) * size
                    total_risk_usd += trade_risk_usd
            except Exception:
                continue
        return total_risk_usd
    # =========================================================================

    def check_safety_filters(
        self,
        daily_pnl_usd: float,
        active_positions: List[Dict[str, Any]],
        symbol: str,
        exchange_manager: Any = None
    ) -> Tuple[bool, str]:
        settings = self._get_risk_settings()

        if not math.isfinite(daily_pnl_usd):
            self.logger.error("🛑 [RISK ERROR] daily_pnl_usd is not finite")
            return False, "INVALID_DAILY_PNL"

        max_loss_pct = float(settings.get("max_daily_loss_pct", 2.0))
        max_loss_usd = self.balance * (max_loss_pct / 100.0)

        if daily_pnl_usd < 0 and abs(daily_pnl_usd) >= max_loss_usd:
            self.logger.error(
                f"🛑 [CIRCUIT BREAKER] Daily loss limit hit: "
                f"{abs(daily_pnl_usd):.2f}/{max_loss_usd:.2f} USD"
            )
            return False, "DAILY_LOSS_LIMIT_HIT"

        max_trades = int(settings.get("max_open_trades", 3))
        if len(active_positions) >= max_trades:
            self.logger.warning(
                f"⚠️ [RISK BLOCK] Max open positions reached: "
                f"{len(active_positions)}/{max_trades}"
            )
            return False, "MAX_TRADES_LIMIT"

        for pos in active_positions:
            try:
                size = float(pos.get("size", 0) or 0)
                stop_loss = float(pos.get("stop_loss", pos.get("stopLoss", 0)) or 0)
            except Exception:
                continue

            if size > 0 and stop_loss <= 0:
                symbol_name = pos.get("symbol", "UNKNOWN")
                self.logger.error(
                    f"🛑 [RISK BLOCK] Active position without stop loss detected: {symbol_name}"
                )
                return False, "POSITION_WITHOUT_STOP"

        # --- [INSTITUTIONAL SCALING] Проверка Portfolio Heat ---
        # Максимально допустимый суммарный риск (например, 6% от депо)
        max_portfolio_heat_pct = float(self._get_global_settings().get("max_portfolio_heat_pct", 6.0))
        max_heat_usd = self.balance * (max_portfolio_heat_pct / 100.0)
        
        current_heat_usd = self._calculate_portfolio_heat(active_positions)
        risk_pct = float(settings.get("risk_per_trade_pct", 1.0))
        new_trade_risk_usd = self.balance * (risk_pct / 100.0)

        if (current_heat_usd + new_trade_risk_usd) > max_heat_usd:
            self.logger.warning(
                f"⚠️ [RISK BLOCK] Portfolio Heat limit reached. "
                f"Current: {current_heat_usd:.2f} + New: {new_trade_risk_usd:.2f} > Max: {max_heat_usd:.2f}"
            )
            return False, "PORTFOLIO_HEAT_LIMIT"
        # -------------------------------------------------------

        normalized_symbol = self._normalize_symbol(symbol)

        # 1. Защита от дублирования активных позиций
        for pos in active_positions:
            pos_symbol = self._normalize_symbol(str(pos.get("symbol", "")))
            if pos_symbol == normalized_symbol:
                self.logger.warning(
                    f"⚠️ [RISK BLOCK] Duplicate asset exposure blocked: {symbol}"
                )
                return False, "DUPLICATE_ASSET"

        # 2. НОВАЯ ЗАЩИТА: Проверяем, нет ли уже выставленной ЛИМИТКИ в стакане Bybit
        if exchange_manager is not None:
            try:
                res = exchange_manager.session.get_open_orders(category="linear", symbol=symbol)
                open_orders = res.get("result", {}).get("list", [])
                
                for order in open_orders:
                    if order.get("orderType") == "Limit" and order.get("orderStatus") in ["New", "PartiallyFilled"]:
                        self.logger.warning(
                            f"⚠️ [RISK BLOCK] Pending LIMIT order already exists for {symbol}. Skipping to avoid duplicate exposure."
                        )
                        return False, "DUPLICATE_PENDING_LIMIT"
            except Exception as e:
                self.logger.error(f"⚠️ [RISK ERROR] Failed to verify pending orders for {symbol}: {e}")

        return True, "SAFE"

    def calculate_lot_size(
        self,
        side: str,
        entry_price: float,
        stop_loss: float,
        available_balance: Optional[float] = None,
        qty_precision: int = 4
    ) -> Tuple[float, float]:
        if not all(math.isfinite(x) and x > 0 for x in [entry_price, stop_loss]):
            self.logger.error("🛑 [RISK ERROR] Invalid entry or stop price")
            return 0.0, stop_loss

        direction = side.lower().strip()
        if direction not in ("buy", "long", "sell", "short"):
            self.logger.error(f"🛑 [RISK ERROR] Invalid side: {side}")
            return 0.0, stop_loss

        is_long = direction in ("buy", "long")

        if is_long and stop_loss >= entry_price:
            self.logger.error(
                f"🛑 [RISK INVALID] LONG stop must be below entry: "
                f"SL={stop_loss}, ENTRY={entry_price}"
            )
            return 0.0, stop_loss

        if not is_long and stop_loss <= entry_price:
            self.logger.error(
                f"🛑 [RISK INVALID] SHORT stop must be above entry: "
                f"SL={stop_loss}, ENTRY={entry_price}"
            )
            return 0.0, stop_loss

        risk_settings = self._get_risk_settings()
        global_settings = self._get_global_settings()

        risk_pct = float(risk_settings.get("risk_per_trade_pct", 1.0))
        leverage = int(global_settings.get("leverage", 10))

        if risk_pct <= 0:
            self.logger.error("🛑 [RISK ERROR] risk_per_trade_pct must be positive")
            return 0.0, stop_loss

        if leverage <= 0:
            self.logger.error("🛑 [RISK ERROR] leverage must be positive")
            return 0.0, stop_loss

        # --- [АДАПТИВНАЯ ПРОВЕРКА БАЛАНСА] ---
        current_available = float(available_balance) if available_balance is not None else self.balance
        
        if current_available <= 0 or not math.isfinite(current_available):
            self.logger.warning(f"⚠️ [RISK] API баланс нулевой, используем кэшированный: {self.balance:.2f}")
            current_available = self.balance
            
        if current_available <= 0:
            self.logger.error("🛑 [RISK ERROR] Баланс отсутствует совсем")
            return 0.0, stop_loss
        # --------------------------------------

        stop_dist = abs(entry_price - stop_loss)
        stop_dist_pct = (stop_dist / entry_price) * 100.0

        if stop_dist_pct < self.MIN_STOP_PCT:
            self.logger.warning(
                f"⚠️ [RISK] Stop too tight: {stop_dist_pct:.4f}%. "
                f"Adjusted to protective minimum {self.MIN_STOP_PCT:.2f}% to avoid noise."
            )

            stop_dist = entry_price * (self.MIN_STOP_PCT / 100.0)
            stop_loss = entry_price - stop_dist if is_long else entry_price + stop_dist

        risk_amount_usd = self.balance * (risk_pct / 100.0)

        qty = risk_amount_usd / stop_dist
        notional_value = qty * entry_price
        required_margin = notional_value / leverage

        if required_margin > current_available:
            max_allowed_margin = current_available * self.MARGIN_BUFFER
            max_notional = max_allowed_margin * leverage

            if max_notional < self.MIN_POSITION_VALUE_USD:
                self.logger.error(
                    f"🛑 [RISK BLOCK] Not enough margin. "
                    f"Available={current_available:.2f}, MaxNotional={max_notional:.2f}"
                )
                return 0.0, stop_loss

            qty = max_notional / entry_price
            notional_value = qty * entry_price

            self.logger.warning(
                f"🚨 [RISK SIZING DOWN] Position reduced by margin limit. "
                f"Qty={qty:.8f}, Notional={notional_value:.2f}"
            )

        if notional_value < self.MIN_POSITION_VALUE_USD:
            self.logger.info(
                f"ℹ️ [RISK BLOCK] Notional below exchange minimum: "
                f"{notional_value:.2f} < {self.MIN_POSITION_VALUE_USD:.2f}"
            )
            return 0.0, stop_loss

        qty = self._round_down(qty, qty_precision)

        if qty <= 0:
            self.logger.error("🛑 [RISK ERROR] Qty rounded to zero")
            return 0.0, stop_loss

        return qty, round(stop_loss, 6)

    def get_min_score(self) -> int:
        settings = self._get_risk_settings()
        return int(settings.get("min_score_to_enter", 25))

    def validate_risk_reward(
        self,
        entry: float,
        stop: float,
        tp: float,
        score: int = 0,
        min_rr: Optional[float] = None
    ) -> str:
        if not all(math.isfinite(x) and x > 0 for x in [entry, stop, tp]):
            return "REJECT"

        risk = abs(entry - stop)
        reward = abs(tp - entry)

        if risk <= 0:
            return "REJECT"

        rr_ratio = reward / risk

        if min_rr is None:
            config_ratios = config.TRADE_EXECUTION.get("tp_ratios", [])
            min_rr = float(config_ratios[0]) if config_ratios else self.DEFAULT_MIN_RR

        if rr_ratio >= min_rr:
            return "MARKET"

        if rr_ratio < min_rr and score >= 50:
            self.logger.info(f"ℹ️ [RISK ROUTER] R:R low ({rr_ratio:.2f}), но высокий Score ({score}). Routing to LIMIT.")
            return "LIMIT"

        self.logger.warning(
            f"⚠️ [R:R REJECT] RR too low: {rr_ratio:.2f} < {min_rr:.2f} (Low score fallback)"
        )
        return "REJECT"
