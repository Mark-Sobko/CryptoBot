import logging
import math
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Dict, Any, Optional

import config


class InstrumentCache:
    """
    [INSTITUTIONAL INSTRUMENT CACHE v1.1]
    Кэш спецификаций Bybit:
    - tickSize
    - qtyStep
    - minQty / maxQty
    - minNotionalValue
    - безопасное округление qty/price
    Расширен: добавлена инвалидация кэша (TTL) и валидация максимального объема ордера.
    """

    CATEGORY = "linear"

    def __init__(self, session):
        self.session = session
        self.logger = logging.getLogger("SMC_BOT.InstrumentCache")
        self.cache: Dict[str, Dict[str, Any]] = {}

        global_cfg = config.RISK_MANAGEMENT.get("global", {})
        self.retry_attempts = int(global_cfg.get("retry_attempts", 3))
        
        # [INSTITUTIONAL SCALING] Время жизни кэша в секундах (24 часа)
        self.cache_ttl = 86400 

    def _api_call(self, func, *args, **kwargs) -> Optional[Dict[str, Any]]:
        for attempt in range(1, self.retry_attempts + 1):
            try:
                res = func(*args, **kwargs)

                if isinstance(res, dict) and res.get("retCode") == 0:
                    return res

                self.logger.warning(
                    f"⚠️ [BYBIT API] Instrument request failed "
                    f"{attempt}/{self.retry_attempts}: {res}"
                )

            except Exception as e:
                self.logger.warning(
                    f"⚠️ [BYBIT API EXCEPTION] Instrument request "
                    f"{attempt}/{self.retry_attempts}: {e}"
                )

            if attempt < self.retry_attempts:
                time.sleep(min(2 ** attempt, 5))

        return None

    def refresh(self, symbol: str) -> bool:
        res = self._api_call(
            self.session.get_instruments_info,
            category=self.CATEGORY,
            symbol=symbol,
        )

        if not res or not res.get("result", {}).get("list"):
            self.logger.error(f"🛑 [{symbol}] Failed to fetch instrument info or list is empty")
            return False

        try:
            info = res["result"]["list"][0]
            price_filter = info["priceFilter"]
            lot_filter = info["lotSizeFilter"]

            self.cache[symbol] = {
                "tickSize": str(price_filter["tickSize"]),
                "qtyStep": str(lot_filter["qtyStep"]),
                "minQty": str(lot_filter["minOrderQty"]),
                "maxQty": str(lot_filter.get("maxOrderQty", "999999999")), # [INSTITUTIONAL SCALING]
                "minNotionalValue": str(lot_filter.get("minNotionalValue", "0")),
                "updated_at": time.time(), # [INSTITUTIONAL SCALING]
            }

            return True

        except Exception as e:
            self.logger.error(f"🛑 [{symbol}] Instrument parse error: {e}")
            return False

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        # =========================================================================
        # [INSTITUTIONAL SCALING] Авто-инвалидация старого кэша
        # =========================================================================
        if symbol in self.cache:
            age = time.time() - self.cache[symbol].get("updated_at", 0)
            if age > self.cache_ttl:
                self.logger.info(f"🔄 [{symbol}] Instrument cache expired ({age:.0f}s). Refreshing...")
                self.refresh(symbol)
        # =========================================================================

        if symbol not in self.cache:
            if not self.refresh(symbol):
                return None

        return self.cache[symbol]

    @staticmethod
    def _round_step(value: float, step: str, mode: str) -> float:
        if value <= 0 or not math.isfinite(value):
            return 0.0

        d_value = Decimal(str(value))
        d_step = Decimal(str(step))

        if d_step <= 0:
            return float(value)

        units = d_value / d_step

        if mode == "down":
            rounded = units.to_integral_value(rounding=ROUND_DOWN) * d_step
        elif mode == "up":
            rounded = units.to_integral_value(rounding=ROUND_UP) * d_step
        else:
            rounded = round(units) * d_step

        places = max(0, -d_step.as_tuple().exponent)
        return float(round(rounded, places))

    def normalize_qty(self, symbol: str, qty: float) -> float:
        specs = self.get(symbol)
        if not specs:
            return 0.0

        return self._round_step(qty, specs["qtyStep"], "down")

    def normalize_price_down(self, symbol: str, price: float) -> float:
        specs = self.get(symbol)
        if not specs:
            return 0.0

        return self._round_step(price, specs["tickSize"], "down")

    def normalize_price_up(self, symbol: str, price: float) -> float:
        specs = self.get(symbol)
        if not specs:
            return 0.0

        return self._round_step(price, specs["tickSize"], "up")

    def normalize_price_nearest(self, symbol: str, price: float) -> float:
        specs = self.get(symbol)
        if not specs:
            return 0.0

        return self._round_step(price, specs["tickSize"], "nearest")

    def normalize_stop(self, symbol: str, sl: float, side: str) -> float:
        side = side.upper()

        if side in ("LONG", "BUY"):
            return self.normalize_price_down(symbol, sl)

        if side in ("SHORT", "SELL"):
            return self.normalize_price_up(symbol, sl)

        return 0.0

    def normalize_tp(self, symbol: str, tp: float, side: str) -> float:
        side = side.upper()

        if side in ("LONG", "BUY"):
            return self.normalize_price_down(symbol, tp)

        if side in ("SHORT", "SELL"):
            return self.normalize_price_up(symbol, tp)

        return 0.0

    def validate_order_size(
        self,
        symbol: str,
        qty: float,
        price: float,
    ) -> bool:
        specs = self.get(symbol)
        if not specs:
            return False

        min_qty = float(specs["minQty"])
        max_qty = float(specs.get("maxQty", float("inf"))) # [INSTITUTIONAL SCALING]
        min_notional = float(specs.get("minNotionalValue", 0) or 0)

        if qty < min_qty:
            self.logger.warning(
                f"⚠️ [{symbol}] Qty below minQty: {qty} < {min_qty}"
            )
            return False
            
        # [INSTITUTIONAL SCALING] Проверка максимального объема
        if qty > max_qty:
            self.logger.warning(
                f"⚠️ [{symbol}] Qty above maxOrderQty: {qty} > {max_qty}"
            )
            return False

        notional = qty * price

        if min_notional > 0 and notional < min_notional:
            self.logger.warning(
                f"⚠️ [{symbol}] Notional below minNotional: "
                f"{notional:.2f} < {min_notional:.2f}"
            )
            return False

        return True