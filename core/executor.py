import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

import config
from core.logger import TradeLogger
from core.instrument_cache import InstrumentCache
from core.tp_manager import TPManager
from core.position_manager import PositionManager
from core.database_sync import DatabaseSync


class TradeExecutor:
    """
    [INSTITUTIONAL EXECUTION FACADE v5.0]

    Совместимый фасад исполнения:
    - вход в сделку (Market / Limit)
    - постановка SL
    - постановка каскадных TP
    - аудит JSONL
    - запись SQLite
    - сопровождение позиции
    - [NEW] Adaptive Slippage Control
    """

    CATEGORY = "linear"

    def __init__(self, exchange_manager):
        self.ex = exchange_manager
        self.session = exchange_manager.session

        self.logger = logging.getLogger("SMC_BOT.ExecutionEngine")
        self.audit = TradeLogger()

        self.instruments = InstrumentCache(self.session)
        self.tp_manager = TPManager(self.session, self.instruments)
        self.position_manager = PositionManager(self.session, self.instruments)
        self.database_sync = DatabaseSync()

        global_cfg = config.RISK_MANAGEMENT.get("global", {})
        self.retry_attempts = int(global_cfg.get("retry_attempts", 3))
        self.request_delay = 0.3
        
        # Институциональный допуск проскальзывания для рыночных ордеров (0.2%)
        self.max_slippage_pct = float(global_cfg.get("max_slippage_pct", 0.2))

    def _api_call(self, func, *args, **kwargs) -> Optional[Dict[str, Any]]:
        for attempt in range(1, self.retry_attempts + 1):
            try:
                res = func(*args, **kwargs)

                if isinstance(res, dict) and res.get("retCode") == 0:
                    return res

                self.logger.warning(
                    f"⚠️ [BYBIT API] Execution request failed "
                    f"{attempt}/{self.retry_attempts}: {res}"
                )

            except Exception as e:
                self.logger.warning(
                    f"⚠️ [BYBIT API EXCEPTION] Execution request "
                    f"{attempt}/{self.retry_attempts}: {e}"
                )

            if attempt < self.retry_attempts:
                time.sleep(min(2 ** attempt, 5))

        return None

    @staticmethod
    def _normalize_side(side: str) -> Optional[Tuple[str, str, int]]:
        side_upper = str(side).upper().strip()

        if side_upper in ("LONG", "BUY"):
            return "LONG", "Buy", 1

        if side_upper in ("SHORT", "SELL"):
            return "SHORT", "Sell", 2

        return None

    def _get_last_price(self, symbol: str) -> Optional[float]:
        res = self._api_call(
            self.session.get_tickers,
            category=self.CATEGORY,
            symbol=symbol,
        )

        if not res:
            return None

        try:
            ticker = res["result"]["list"][0]
            price = float(ticker["lastPrice"])

            if price <= 0 or not math.isfinite(price):
                return None

            return price

        except Exception as e:
            self.logger.error(f"🛑 [{symbol}] Ticker parse error: {e}")
            return None

    def get_tp_levels(
        self,
        entry: float,
        stop: float,
        side: str,
    ) -> Dict[str, float]:
        return self.tp_manager.calculate_tp_levels(entry, stop, side)

    def _validate_entry(
        self,
        symbol: str,
        trade_side: str,
        entry: float,
        sl: float,
        tp_levels: Dict[str, float],
    ) -> bool:
        if entry <= 0 or sl <= 0:
            self.logger.error(
                f"🛑 [{symbol}] Invalid entry/sl: entry={entry}, sl={sl}"
            )
            return False

        if trade_side == "LONG" and sl >= entry:
            self.logger.error(
                f"🛑 [{symbol}] LONG stop must be below entry: sl={sl}, entry={entry}"
            )
            return False

        if trade_side == "SHORT" and sl <= entry:
            self.logger.error(
                f"🛑 [{symbol}] SHORT stop must be above entry: sl={sl}, entry={entry}"
            )
            return False

        return self.tp_manager.validate_tp_levels(
            symbol=symbol,
            entry=entry,
            side=trade_side,
            tp_levels=tp_levels,
        )

    # =========================================================================
    # [INSTITUTIONAL SCALING] Контроль проскальзывания
    # =========================================================================
    def _check_slippage(self, symbol: str, theoretical_price: float, current_price: float) -> bool:
        """
        Отменяет рыночный вход, если цена улетела слишком далеко от расчетной.
        """
        if theoretical_price <= 0 or current_price <= 0:
            return False
            
        diff_pct = abs(current_price - theoretical_price) / theoretical_price * 100
        
        if diff_pct > self.max_slippage_pct:
            self.logger.warning(
                f"🛑 [SLIPPAGE GUARD] {symbol} Price moved too far. "
                f"Expected: {theoretical_price}, Actual: {current_price}. Diff: {diff_pct:.2f}%"
            )
            return False
            
        return True

    @staticmethod
    def _get_poi_reference_price(poi: Dict[str, Any]) -> Optional[float]:
        if not isinstance(poi, dict):
            return None

        for key in ("price", "mid"):
            try:
                value = float(poi.get(key, 0) or 0)
                if value > 0 and math.isfinite(value):
                    return value
            except Exception:
                pass

        try:
            top = float(poi.get("top", 0) or 0)
            bottom = float(poi.get("bottom", 0) or 0)
            midpoint = (top + bottom) / 2.0
            if top > 0 and bottom > 0 and midpoint > 0 and math.isfinite(midpoint):
                return midpoint
        except Exception:
            return None

        return None
    # =========================================================================

    @staticmethod
    def _make_order_link_id(symbol: str, order_type: str) -> str:
        ts = int(time.time() * 1000)
        clean_symbol = "".join(ch for ch in str(symbol).upper() if ch.isalnum())
        clean_type = str(order_type).upper()[0]
        return f"smc-{clean_symbol}-{clean_type}-{ts}"[:36]

    def execute_institutional_entry(
        self,
        symbol: str,
        side: str,
        poi: Dict[str, Any],
        score: int,
        qty: float,
        sl: float,
        risk_pct: float,
        order_type: str = "Market",          # Внедряем тип ордера
        limit_price: Optional[float] = None  # Внедряем цену исполнения
    ) -> Optional[Dict[str, Any]]:
        try:
            order_type = "Limit" if str(order_type).lower() == "limit" else "Market"
            side_info = self._normalize_side(side)
            if side_info is None:
                self.logger.error(f"🛑 [{symbol}] Invalid side: {side}")
                return None

            trade_side, order_side, position_idx = side_info

            if not self.instruments.refresh(symbol):
                self.logger.error(f"🛑 [{symbol}] Failed to refresh instrument specs")
                return None

            # -----------------------------------------------------------------
            # Логика получения цены и контроль проскальзывания
            # -----------------------------------------------------------------
            current_market_price = self._get_last_price(symbol)
            if current_market_price is None or current_market_price <= 0:
                self.logger.error(f"🛑 [{symbol}] Failed to get market price")
                return None

            if order_type == "Limit":
                entry_price = float(limit_price) if limit_price else current_market_price
            else:
                entry_price = current_market_price
                
                expected_poi_price = self._get_poi_reference_price(poi)
                if expected_poi_price is not None:
                    if not self._check_slippage(symbol, expected_poi_price, entry_price):
                        return None
            # -----------------------------------------------------------------

            normalized_qty = self.instruments.normalize_qty(symbol, float(qty))

            if normalized_qty <= 0:
                self.logger.warning(f"❌ [{symbol}] Normalized qty is zero")
                return None

            if not self.instruments.validate_order_size(
                symbol=symbol,
                qty=normalized_qty,
                price=entry_price,
            ):
                return None

            normalized_sl = self.instruments.normalize_stop(symbol, float(sl), trade_side)

            raw_tp_levels = self.tp_manager.calculate_tp_levels(
                entry=entry_price,
                stop=normalized_sl,
                side=trade_side,
            )

            tp_levels = self.tp_manager.normalize_tp_levels(
                symbol=symbol,
                tp_levels=raw_tp_levels,
                side=trade_side,
            )

            if not self._validate_entry(
                symbol=symbol,
                trade_side=trade_side,
                entry=entry_price,
                sl=normalized_sl,
                tp_levels=tp_levels,
            ):
                return None

            self.logger.info(
                f"🚀 [ORDER SEND] {symbol} | type={order_type} | side={order_side} | "
                f"qty={normalized_qty} | entry≈{entry_price} | "
                f"SL={normalized_sl} | posIdx={position_idx}"
            )

            # --- ИСПРАВЛЕННЫЙ БЛОК ОТПРАВКИ ОРДЕРА С ВШИТЫМ ТЕЙКОМ ---
            take_profit_price = None
            if order_type == "Limit" and tp_levels:
                # Находим TP1 (минимальный таргет из каскада)
                first_tp_key = sorted(tp_levels.keys())[0]
                take_profit_price = str(tp_levels[first_tp_key])

            order_link_id = self._make_order_link_id(symbol, order_type)
            order_params = {
                "category": self.CATEGORY,
                "symbol": symbol,
                "side": order_side,
                "orderType": order_type,
                "qty": str(normalized_qty),
                "stopLoss": str(normalized_sl),
                "slTriggerBy": "LastPrice",
                "tpslMode": "Full",
                "positionIdx": position_idx,
                "orderLinkId": order_link_id,
            }

            if order_type == "Limit":
                order_params["price"] = str(entry_price)
                order_params["timeInForce"] = "GTC"

                if take_profit_price:
                    order_params["takeProfit"] = take_profit_price
                    order_params["tpTriggerBy"] = "LastPrice"
            else:
                order_params["timeInForce"] = "IOC"

            response = self._api_call(self.session.place_order, **order_params)

            if not response:
                self.logger.error(f"❌ [{symbol}] {order_type} order rejected or failed")
                self.audit.log_trade_event(
                    "ORDER_REJECTED",
                    symbol,
                    {
                        "side": trade_side,
                        "qty": normalized_qty,
                        "sl": normalized_sl,
                        "score": int(score),
                    },
                )
                return None

            order_id = str(response.get("result", {}).get("orderId", ""))

            self.logger.info(
                f"✅ [{symbol}] {order_type.upper()} ORDER PLACED | orderId={order_id}"
            )

            # Выставляем каскад Тейков СРАЗУ только для Market-ордеров.
            # Для Лимиток каскад раскидает PositionManager, когда ордер полностью нальют.
            if order_type == "Market":
                tp_ok = self.tp_manager.place_cascade_tps(
                    symbol=symbol,
                    side=trade_side,
                    total_qty=normalized_qty,
                    tp_levels=tp_levels,
                    position_idx=position_idx,
                )
            else:
                tp_ok = True  # Базовый лимитный TP1 уже улетел на биржу внутри ордера входа
            # -----------------------------------------------------------------------

            self.position_manager.remember_position(
                symbol=symbol,
                side=trade_side,
                initial_qty=normalized_qty,
                entry_price=entry_price,
                sl=normalized_sl,
                position_idx=position_idx,
                tps_placed=(order_type == "Market" and bool(tp_ok)),
            )

            poi_type = (
                poi.get("type", "SMC_Zone")
                if isinstance(poi, dict)
                else "SMC_Zone"
            )

            trade_data = {
                "order_id": order_id,
                "order_link_id": order_link_id,
                "entry_price": entry_price,
                "qty": normalized_qty,
                "sl": normalized_sl,
                "tp_levels": tp_levels,
                "side": trade_side,
                "score": int(score),
                "risk_pct": float(risk_pct),
                "poi_type": poi_type,
                "tp_orders_ok": bool(tp_ok),
                "rr_base": float(
                    config.TRADE_EXECUTION.get("tp_ratios", [1.0, 3.0, 5.0])[0]
                ),
                "ts": datetime.now(timezone.utc).isoformat(),
            }

            self.audit.log_trade_event("ORDER_EXECUTED", symbol, trade_data)

            saved = self.database_sync.save_open_trade(
                symbol=symbol,
                side=trade_side,
                entry=entry_price,
                qty=normalized_qty,
                sl=normalized_sl,
                score=int(score),
                poi_type=poi_type,
                order_id=order_id,
                status="PENDING_ORDER" if order_type == "Limit" else "OPEN",
            )

            if not saved:
                self.logger.critical(
                    f"🚨 [{symbol}] Order placed but SQLite persistence failed | orderId={order_id}"
                )
                self.audit.log_trade_event(
                    "DB_PERSISTENCE_FAILED",
                    symbol,
                    {
                        "order_id": order_id,
                        "order_link_id": order_link_id,
                        "side": trade_side,
                        "qty": normalized_qty,
                    },
                )

            if not tp_ok:
                self.logger.warning(
                    f"⚠️ [{symbol}] Entry executed but one or more TP orders failed"
                )
                self.audit.log_trade_event(
                    "TP_PLACEMENT_WARNING",
                    symbol,
                    {
                        "order_id": order_id,
                        "tp_levels": tp_levels,
                    },
                )

            return response

        except Exception as e:
            self.logger.error(
                f"❌ [{symbol}] EXECUTION CRITICAL ERROR: {e}",
                exc_info=True,
            )
            self.audit.log_trade_event(
                "ERROR",
                symbol,
                {
                    "stage": "execute_institutional_entry",
                    "error": str(e),
                },
            )
            return None

    def manage_position_pro(self, pos: Dict[str, Any]) -> None:
        self.position_manager.manage_position(pos)

    def _get_position_idx(self, side: str) -> int:
        side_info = self._normalize_side(side)
        return side_info[2] if side_info else 0
