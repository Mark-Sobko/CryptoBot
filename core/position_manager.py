import logging
import time
from typing import Dict, List, Optional, Any

import config
from core.instrument_cache import InstrumentCache
from core.logger import TradeLogger
from core.database_sync import DatabaseSync
from core.tp_manager import TPManager # [INSTITUTIONAL SCALING] Импорт для расстановки каскадов


class PositionManager:
    """
    [INSTITUTIONAL POSITION MANAGER v1.1]
    Сопровождение открытых позиций:
    - emergency stop
    - break-even
    - partial close detection
    - безопасное изменение SL через Bybit V5
    Расширен: добавлена авто-расстановка пропущенных каскадов TP для лимитных ордеров.
    """

    CATEGORY = "linear"

    def __init__(self, session, instruments: InstrumentCache):
        self.session = session
        self.instruments = instruments
        self.logger = logging.getLogger("SMC_BOT.PositionManager")
        self.audit = TradeLogger()
        self.database_sync = DatabaseSync()
        
        # Инициализируем TPManager для расстановки каскадов
        self.tp_manager = TPManager(self.session, self.instruments)

        self.position_cache: Dict[str, Dict[str, Any]] = {}

        global_cfg = config.RISK_MANAGEMENT.get("global", {})
        self.retry_attempts = int(global_cfg.get("retry_attempts", 3))

    def remember_position(
        self,
        symbol: str,
        side: str,
        initial_qty: float,
        entry_price: float,
        sl: float,
        position_idx: int,
        tps_placed: bool = False,
    ) -> None:
        self.position_cache[symbol] = {
            "symbol": symbol,
            "side": side,
            "initial_qty": float(initial_qty),
            "entry_price": float(entry_price),
            "sl": float(sl),
            "position_idx": int(position_idx),
            "tps_placed": bool(tps_placed)
        }

    def _api_call(self, func, *args, **kwargs) -> Optional[Dict[str, Any]]:
        for attempt in range(1, self.retry_attempts + 1):
            try:
                res = func(*args, **kwargs)

                if isinstance(res, dict) and res.get("retCode") == 0:
                    return res

                self.logger.warning(
                    f"⚠️ [BYBIT API] Position request failed "
                    f"{attempt}/{self.retry_attempts}: {res}"
                )

            except Exception as e:
                self.logger.warning(
                    f"⚠️ [BYBIT API EXCEPTION] Position request "
                    f"{attempt}/{self.retry_attempts}: {e}"
                )

            if attempt < self.retry_attempts:
                time.sleep(min(2 ** attempt, 5))

        return None

    def _load_cached_position(self, symbol: str, side: str) -> Optional[Dict[str, Any]]:
        cached = self.position_cache.get(symbol)
        if cached and cached.get("side") == side:
            return cached

        trade = self.database_sync.get_open_trade(symbol=symbol, side=side)
        if not trade:
            return None

        self.remember_position(
            symbol=symbol,
            side=side,
            initial_qty=float(trade.get("qty", 0.0) or 0.0),
            entry_price=float(trade.get("entry_price", 0.0) or 0.0),
            sl=float(trade.get("stop_loss", 0.0) or 0.0),
            position_idx=self.get_position_idx(side),
            tps_placed=False,
        )
        return self.position_cache.get(symbol)

    @staticmethod
    def normalize_side(side: str) -> Optional[str]:
        side_upper = str(side).upper().strip()

        if side_upper in ("LONG", "BUY"):
            return "LONG"

        if side_upper in ("SHORT", "SELL"):
            return "SHORT"

        return None

    @staticmethod
    def get_position_idx(side: str) -> int:
        side_upper = str(side).upper().strip()

        if side_upper in ("LONG", "BUY"):
            return 1

        if side_upper in ("SHORT", "SELL"):
            return 2

        return 0

    def manage_position(self, pos: Dict[str, Any]) -> None:
        try:
            if not pos:
                return

            symbol = str(pos.get("symbol", ""))
            side_raw = str(pos.get("side", "Buy"))
            
            # --- ВНЕДРЕННАЯ ЛОГИКА ЗАКРЫТИЯ В БД ---
            current_size = abs(float(pos.get("size", 0.0) or 0.0))
            if current_size <= 0:
                self.logger.info(f"✅ [{symbol}] Позиция закрыта. Синхронизация с БД...")
                self.database_sync.close_trade(
                    symbol=symbol,
                    exit_price=float(pos.get("mark_price", pos.get("markPrice", 0.0))),
                    pnl_usd=float(pos.get("unrealisedPnl", 0.0)),
                    pnl_pct=0.0
                )
                return
            # --------------------------------------

            side = self.normalize_side(side_raw)
            if side is None:
                self.logger.warning(f"⚠️ [{symbol}] Unknown position side: {side_raw}")
                return

            position_idx = int(pos.get("positionIdx", self.get_position_idx(side)) or self.get_position_idx(side))

            entry_price = float(pos.get("entry_price", pos.get("entryPrice", 0.0)) or 0.0)
            current_price = float(pos.get("mark_price", pos.get("markPrice", entry_price)) or 0.0)
            current_sl = float(pos.get("stop_loss", pos.get("stopLoss", 0.0)) or 0.0)

            if entry_price <= 0 or current_price <= 0:
                return

            if not self.instruments.get(symbol):
                return

            if current_sl <= 0:
                self.logger.warning(
                    f"⚠️ [{symbol}] Position without SL detected. Emergency SL placement."
                )
                self.set_emergency_stop(symbol, side, entry_price, position_idx)
                return
                
            # =========================================================================
            # [INSTITUTIONAL SCALING] Проверка и расстановка пропущенных каскадов TP
            # =========================================================================
            self._check_and_place_missing_tps(symbol, side, current_size, entry_price, current_sl, position_idx)
            # =========================================================================

            initial_risk = abs(entry_price - current_sl)
            if initial_risk <= 0:
                return

            current_profit = (
                current_price - entry_price
                if side == "LONG"
                else entry_price - current_price
            )

            if current_profit <= 0:
                return

            current_rr = current_profit / initial_risk
            be_rr_trigger = float(config.TRADE_EXECUTION.get("move_to_be_at_rr", 1.2))

            partially_closed = self.is_partially_closed(symbol, side, current_size)

            if current_rr >= be_rr_trigger or partially_closed:
                self.move_to_break_even(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    current_sl=current_sl,
                    initial_risk=initial_risk,
                    position_idx=position_idx,
                )

        except Exception as e:
            symbol = pos.get("symbol", "UNKNOWN") if isinstance(pos, dict) else "UNKNOWN"
            self.logger.error(f"❌ [{symbol}] Position management error: {e}", exc_info=True)

    # =========================================================================
    # [INSTITUTIONAL SCALING] Метод довыставления каскадов
    # =========================================================================
    def _check_and_place_missing_tps(
        self, 
        symbol: str, 
        side: str, 
        current_size: float, 
        entry_price: float, 
        current_sl: float, 
        position_idx: int
    ) -> None:
        """
        Проверяет, расставлен ли каскад TP для позиции.
        Если это сработавший лимитный ордер (у которого есть только 1 вшитый TP),
        метод снимет базовый TP и выставит правильный каскад лимиток.
        """
        cached = self._load_cached_position(symbol, side)
        
        # Если позиция есть в кэше и флаг tps_placed == False
        if cached and cached.get("side") == side and not cached.get("tps_placed"):
            initial_qty = float(cached.get("initial_qty", 0.0))
            
            # Если позиция налита полностью (или почти полностью)
            if initial_qty > 0 and current_size >= (initial_qty * 0.9):
                self.logger.info(f"🔄 [{symbol}] Limit order filled. Placing cascade TPs...")
                
                # 1. Рассчитываем и нормализуем уровни
                raw_tp_levels = self.tp_manager.calculate_tp_levels(entry_price, current_sl, side)
                tp_levels = self.tp_manager.normalize_tp_levels(symbol, raw_tp_levels, side)
                
                # 2. Выставляем каскад (TPManager сам отменит старые тейки)
                if tp_levels:
                    success = self.tp_manager.place_cascade_tps(
                        symbol=symbol,
                        side=side,
                        total_qty=initial_qty, # Рассчитываем каскад от изначального объема
                        tp_levels=tp_levels,
                        position_idx=position_idx,
                    )
                    
                    if success:
                        self.logger.info(f"✅ [{symbol}] Cascade TPs placed successfully.")
                        self.position_cache[symbol]["tps_placed"] = True
                    else:
                        self.logger.warning(f"⚠️ [{symbol}] Failed to place cascade TPs.")
    # =========================================================================

    def is_partially_closed(
        self,
        symbol: str,
        side: str,
        current_size: float,
    ) -> bool:
        cached = self.position_cache.get(symbol)
        if not cached:
            cached = self._load_cached_position(symbol, side)

        if cached and cached.get("side") == side:
            initial_qty = float(cached.get("initial_qty", 0.0) or 0.0)
            if initial_qty > 0 and current_size <= initial_qty * 0.6:
                return True

        try:
            from core.database import TradeDatabase

            db = TradeDatabase()
            try:
                cursor = db.conn.cursor()
                cursor.execute(
                    """
                    SELECT qty FROM trades
                    WHERE symbol = ? AND side = ? AND status = 'OPEN'
                    ORDER BY entry_time DESC
                    LIMIT 1
                    """,
                    (symbol, side),
                )
                row = cursor.fetchone()
            finally:
                db.close()

            if row:
                initial_qty = float(row[0])
                return current_size <= initial_qty * 0.6

        except Exception as e:
            self.logger.debug(f"[{symbol}] Partial-close DB check failed: {e}")

        return False

    def move_to_break_even(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        current_sl: float,
        initial_risk: float,
        position_idx: int,
    ) -> None:
        if side == "LONG":
            if current_sl >= entry_price:
                return

            raw_sl = entry_price + initial_risk * 0.1
            new_sl = self.instruments.normalize_price_down(symbol, raw_sl)

        else:
            if current_sl <= entry_price:
                return

            raw_sl = entry_price - initial_risk * 0.1
            new_sl = self.instruments.normalize_price_up(symbol, raw_sl)

        self.modify_position_sl(symbol, new_sl, position_idx)

    def set_emergency_stop(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        position_idx: int,
    ) -> None:
        if entry_price <= 0:
            return

        if side == "LONG":
            raw_sl = entry_price * 0.98
            sl = self.instruments.normalize_price_down(symbol, raw_sl)
        else:
            raw_sl = entry_price * 1.02
            sl = self.instruments.normalize_price_up(symbol, raw_sl)

        self.modify_position_sl(symbol, sl, position_idx)

    def modify_position_sl(
        self,
        symbol: str,
        new_sl: float,
        position_idx: int,
    ) -> bool:
        if new_sl <= 0:
            return False

        res = self._api_call(
            self.session.set_trading_stop,
            category=self.CATEGORY,
            symbol=symbol,
            stopLoss=str(new_sl),
            slTriggerBy="LastPrice",
            positionIdx=position_idx,
        )

        if res:
            self.logger.info(
                f"🛡️ [{symbol}] SL updated -> {new_sl} | positionIdx={position_idx}"
            )
            self.audit.log_trade_event(
                "SL_UPDATED",
                symbol,
                {
                    "new_sl": new_sl,
                    "positionIdx": position_idx,
                },
            )
            return True

        self.logger.error(f"❌ [{symbol}] SL update failed")
        return False
