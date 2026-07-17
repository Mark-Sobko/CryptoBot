import logging
import math
import time
from decimal import Decimal, ROUND_DOWN
from typing import List, Dict, Any, Tuple, Optional

import config
from core.instrument_cache import InstrumentCache


class TPManager:
    """
    [INSTITUTIONAL TP MANAGER v1.1]
    Расчет и выставление каскадных Take Profit ордеров.
    Расширен: добавлена отмена старых TP-ордеров перед расстановкой нового каскада.
    """

    CATEGORY = "linear"

    def __init__(self, session, instruments: InstrumentCache):
        self.session = session
        self.instruments = instruments
        self.logger = logging.getLogger("SMC_BOT.TPManager")

        global_cfg = config.RISK_MANAGEMENT.get("global", {})
        self.retry_attempts = int(global_cfg.get("retry_attempts", 3))
        self.request_delay = 0.3

    def _api_call(self, func, *args, **kwargs) -> Optional[Dict[str, Any]]:
        for attempt in range(1, self.retry_attempts + 1):
            try:
                res = func(*args, **kwargs)

                if isinstance(res, dict) and res.get("retCode") == 0:
                    return res

                self.logger.warning(
                    f"⚠️ [BYBIT API] TP request failed "
                    f"{attempt}/{self.retry_attempts}: {res}"
                )

            except Exception as e:
                self.logger.warning(
                    f"⚠️ [BYBIT API EXCEPTION] TP request "
                    f"{attempt}/{self.retry_attempts}: {e}"
                )

            if attempt < self.retry_attempts:
                time.sleep(min(2 ** attempt, 5))

        return None

    @staticmethod
    def normalize_side(side: str) -> Optional[str]:
        side_upper = str(side).upper().strip()

        if side_upper in ("LONG", "BUY"):
            return "LONG"

        if side_upper in ("SHORT", "SELL"):
            return "SHORT"

        return None

    def calculate_tp_levels(
        self,
        entry: float,
        stop: float,
        side: str,
    ) -> Dict[str, float]:
        trade_side = self.normalize_side(side)
        if trade_side is None:
            return {}

        risk = abs(entry - stop)
        if risk <= 0:
            return {}

        ratios = config.TRADE_EXECUTION.get("tp_ratios", [1.5, 3.0, 5.0])

        levels: Dict[str, float] = {}

        for i, ratio in enumerate(ratios):
            r = float(ratio)

            if trade_side == "LONG":
                levels[f"tp{i + 1}"] = entry + risk * r
            else:
                levels[f"tp{i + 1}"] = entry - risk * r

        return levels

    def normalize_tp_levels(
        self,
        symbol: str,
        tp_levels: Dict[str, float],
        side: str,
    ) -> Dict[str, float]:
        normalized: Dict[str, float] = {}

        for name, price in tp_levels.items():
            tp = self.instruments.normalize_tp(symbol, price, side)
            if tp > 0:
                normalized[name] = tp

        return normalized

    def validate_tp_levels(
        self,
        symbol: str,
        entry: float,
        side: str,
        tp_levels: Dict[str, float],
    ) -> bool:
        trade_side = self.normalize_side(side)
        if trade_side is None:
            self.logger.error(f"🛑 [{symbol}] Invalid TP side: {side}")
            return False

        if entry <= 0:
            self.logger.error(f"🛑 [{symbol}] Invalid entry for TP validation")
            return False

        if not tp_levels:
            self.logger.error(f"🛑 [{symbol}] Empty TP levels")
            return False

        for name, tp in tp_levels.items():
            if tp <= 0:
                self.logger.error(f"🛑 [{symbol}] Invalid {name}: {tp}")
                return False

            if trade_side == "LONG" and tp <= entry:
                self.logger.error(
                    f"🛑 [{symbol}] LONG {name} must be above entry: {tp} <= {entry}"
                )
                return False

            if trade_side == "SHORT" and tp >= entry:
                self.logger.error(
                    f"🛑 [{symbol}] SHORT {name} must be below entry: {tp} >= {entry}"
                )
                return False

        return True

    def _split_qty(self, symbol: str, total_qty: float, count: int) -> List[float]:
        if total_qty <= 0 or count <= 0:
            return []

        partial_close_pct = float(config.TRADE_EXECUTION.get("partial_close_pct", 0.5))

        if count == 1:
            return [self.instruments.normalize_qty(symbol, total_qty)]

        base_parts = [partial_close_pct]

        remaining_part = max(0.0, 1.0 - partial_close_pct)
        tail_count = count - 1
        tail_part = remaining_part / tail_count if tail_count > 0 else 0.0

        for _ in range(tail_count):
            base_parts.append(tail_part)

        qty_parts: List[float] = []
        used_qty = 0.0

        for i, part in enumerate(base_parts):
            if i == count - 1:
                qty = self.instruments.normalize_qty(symbol, total_qty - used_qty)
            else:
                qty = self.instruments.normalize_qty(symbol, total_qty * part)
                used_qty += qty

            if qty > 0:
                qty_parts.append(qty)

        return qty_parts

    # =========================================================================
    # [INSTITUTIONAL SCALING] Отмена старых тейков перед расстановкой каскада
    # =========================================================================
    def _cancel_existing_tps(self, symbol: str, position_idx: int) -> None:
        """Снимает все текущие ордера закрытия, чтобы освободить место для каскада."""
        try:
            # Снимаем встроенный системный TP (если он был поставлен при входе)
            self._api_call(
                self.session.set_trading_stop,
                category=self.CATEGORY,
                symbol=symbol,
                takeProfit="0", 
                positionIdx=position_idx
            )
            time.sleep(self.request_delay)
            
            # Снимаем все открытые лимитные ордера ReduceOnly по этому символу
            self._api_call(
                self.session.cancel_all_orders,
                category=self.CATEGORY,
                symbol=symbol
            )
            time.sleep(self.request_delay)
        except Exception as e:
            self.logger.debug(f"[{symbol}] Failed to cancel existing TPs: {e}")
    # =========================================================================

    def place_cascade_tps(
        self,
        symbol: str,
        side: str,
        total_qty: float,
        tp_levels: Dict[str, float],
        position_idx: int,
    ) -> bool:
        trade_side = self.normalize_side(side)
        if trade_side is None:
            self.logger.error(f"🛑 [{symbol}] Invalid side for TP placement: {side}")
            return False

        if total_qty <= 0:
            self.logger.error(f"🛑 [{symbol}] Invalid total_qty for TP placement")
            return False

        if not tp_levels:
            self.logger.error(f"🛑 [{symbol}] Empty TP levels for placement")
            return False

        tp_side = "Sell" if trade_side == "LONG" else "Buy"

        # Очищаем старые тейки
        self._cancel_existing_tps(symbol, position_idx)

        ordered_levels = list(tp_levels.items())
        qty_parts = self._split_qty(symbol, total_qty, len(ordered_levels))

        if not qty_parts:
            self.logger.error(f"🛑 [{symbol}] Qty split failed for TP placement")
            return False

        success_count = 0

        for i, ((name, raw_price), part_qty) in enumerate(
            zip(ordered_levels, qty_parts),
            start=1,
        ):
            tp_price = self.instruments.normalize_tp(symbol, raw_price, trade_side)

            if part_qty <= 0 or tp_price <= 0:
                continue

            res = self._api_call(
                self.session.place_order,
                category=self.CATEGORY,
                symbol=symbol,
                side=tp_side,
                orderType="Limit",
                qty=str(part_qty),
                price=str(tp_price),
                reduceOnly=True,
                closeOnTrigger=False,
                positionIdx=position_idx,
                timeInForce="GTC",
            )

            if res:
                success_count += 1
                self.logger.info(
                    f"🎯 [{symbol}] {name.upper()} placed | "
                    f"price={tp_price} | qty={part_qty} | posIdx={position_idx}"
                )
            else:
                self.logger.error(
                    f"❌ [{symbol}] Failed to place {name.upper()} | "
                    f"price={tp_price} | qty={part_qty}"
                )

            time.sleep(self.request_delay)

        return success_count > 0