import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import config


class DatabaseSync:
    """
    [INSTITUTIONAL DATABASE SYNC v1.1]
    Изолированный слой записи и обновления сделок в SQLite.
    Расширен: добавлены методы чтения (Read-Models) для RiskManager'а.
    """

    def __init__(self):
        self.logger = logging.getLogger("SMC_BOT.DatabaseSync")
        self._db = None

    def _get_db(self):
        if self._db is not None:
            return self._db

        from core.database import TradeDatabase

        self._db = TradeDatabase()
        return self._db

    def save_open_trade(
        self,
        symbol: str,
        side: str,
        entry: float,
        qty: float,
        sl: float,
        score: int,
        poi_type: str,
        order_id: str = "",
    ) -> bool:
        try:
            db = self._get_db()

            db_data: Dict[str, Any] = {
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "symbol": str(symbol),
                "side": str(side),
                "entry": float(entry),
                "exit": 0.0,
                "qty": float(qty),
                "pnl_usd": 0.0,
                "pnl_pct": 0.0,
                "score": int(score),
                "poi_type": str(poi_type),
                "rr": float(config.TRADE_EXECUTION.get("tp_ratios", [1.5, 3.0, 5.0])[0]),
                "sl": float(sl),
                "order_id": str(order_id),
            }

            db.add_trade(db_data)
            return True

        except Exception as e:
            self.logger.error(f"❌ [{symbol}] SQLite save_open_trade failed: {e}")
            return False

    def close_trade(
        self, 
        symbol: str, 
        exit_price: float, 
        pnl_usd: float, 
        pnl_pct: float = 0.0, 
        order_id: Optional[str] = None,
        trade_id: Optional[int] = None
    ) -> bool:
        """Обновляет статус сделки до CLOSED."""
        try:
            db = self._get_db()
            return db.close_trade(
                symbol=symbol,
                exit_price=exit_price,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                order_id=order_id,
                trade_id=trade_id
            )
        except Exception as e:
            self.logger.error(f"❌ [{symbol}] SQLite close_trade failed: {e}")
            return False

    # =========================================================================
    # [INSTITUTIONAL SCALING] Методы чтения (Read Models) для Risk Manager
    # =========================================================================
    def get_active_trades_count(self) -> int:
        """Возвращает количество текущих активных (OPEN) сделок из базы."""
        try:
            db = self._get_db()
            cursor = db.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception as e:
            self.logger.debug(f"Failed to fetch active trades count: {e}")
            return 0

    def get_todays_pnl(self) -> float:
        """Считает сумму закрытых PnL (USD) за текущие сутки (UTC)."""
        try:
            db = self._get_db()
            cursor = db.conn.cursor()
            # Фильтруем сделки, закрытые сегодня по UTC
            today_prefix = datetime.now(timezone.utc).isoformat()[:10] # 'YYYY-MM-DD'
            cursor.execute(
                "SELECT SUM(pnl_usd) FROM trades WHERE status = 'CLOSED' AND exit_time LIKE ?",
                (f"{today_prefix}%",)
            )
            row = cursor.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0
        except Exception as e:
            self.logger.debug(f"Failed to fetch today's PNL: {e}")
            return 0.0