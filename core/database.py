import sqlite3
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List

import config


class TradeDatabase:
    """
    [INSTITUTIONAL STORAGE ENGINE v5.0]
    SQLite WAL-хранилище:
    - потокобезопасная запись
    - PENDING_ORDER / OPEN / CLOSED сделки
    - order_id
    - stop_loss
    - восстановление открытых позиций после рестарта
    - дневной PnL
    - scan audit history
    """

    def __init__(self, db_path: str = config.DB_PATH):
        self.logger = logging.getLogger("SMC_BOT.Database")
        self.db_path = str(db_path)
        self._lock = threading.RLock()

        self._prepare_env(self.db_path)

        self.conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=30,
        )

        self.conn.row_factory = sqlite3.Row

        self._setup_engine()
        self._create_tables()
        self._migrate_schema()
        self._create_indexes()

        self.logger.info(f"✅ DB CONNECTED | Path: {self.db_path} | WAL active")

    def _prepare_env(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _setup_engine(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self.conn.execute("PRAGMA foreign_keys=ON;")
            self.conn.execute("PRAGMA cache_size=-64000;")
            self.conn.execute("PRAGMA busy_timeout=30000;")

    def _create_tables(self) -> None:
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    stop_loss REAL,
                    qty REAL NOT NULL,
                    pnl_usd REAL DEFAULT 0.0,
                    pnl_pct REAL DEFAULT 0.0,
                    score INTEGER,
                    poi_type TEXT,
                    rr REAL,
                    status TEXT DEFAULT 'OPEN'
                )
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    symbol TEXT,
                    trend TEXT,
                    score INTEGER,
                    details TEXT
                )
            """)

        self.conn.commit()
    
    def _create_indexes(self) -> None:
        with self._lock:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_status ON trades(symbol, status)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_ts ON scan_history(ts)")
            self.conn.commit()

    def _table_columns(self, table: str) -> List[str]:
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        return [row["name"] for row in cursor.fetchall()]

    def _migrate_schema(self) -> None:
        """
        Безопасная миграция старой таблицы v4.6:
        - добавляет order_id
        - добавляет stop_loss
        """
        with self._lock:
            columns = self._table_columns("trades")

            if "order_id" not in columns:
                self.conn.execute("ALTER TABLE trades ADD COLUMN order_id TEXT")

            if "stop_loss" not in columns:
                self.conn.execute("ALTER TABLE trades ADD COLUMN stop_loss REAL")

            self.conn.commit()

    def add_trade(self, data: Dict[str, Any]) -> Optional[int]:
        """
        Фиксация ордера или открытия позиции.
        Совместимо и со старым ключом 'entry', и с новым 'entry_price'.
        """
        with self._lock:
            try:
                entry_price = float(data.get("entry_price", data.get("entry")))
                order_id = str(data.get("order_id", "") or "") or None
                status = str(data.get("status", "OPEN")).upper()
                allowed_statuses = {
                    "PENDING_ORDER",
                    "OPEN",
                    "CLOSED",
                    "CLOSED_UNVERIFIED",
                    "CANCELLED",
                    "REJECTED",
                }
                if status not in allowed_statuses:
                    self.logger.warning(f"⚠️ DB add_trade: unknown status {status}, falling back to OPEN")
                    status = "OPEN"

                cursor = self.conn.cursor()

                cursor.execute(
                    """
                    INSERT OR IGNORE INTO trades (
                        order_id,
                        entry_time,
                        symbol,
                        side,
                        entry_price,
                        stop_loss,
                        qty,
                        score,
                        poi_type,
                        rr,
                        status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        data.get("entry_time", datetime.now(timezone.utc).isoformat()),
                        str(data["symbol"]),
                        str(data["side"]),
                        entry_price,
                        float(data.get("sl", data.get("stop_loss", 0.0)) or 0.0),
                        float(data["qty"]),
                        int(data.get("score", 0)),
                        str(data.get("poi_type", "SMC_Zone")),
                        float(data.get("rr", 0.0)),
                        status,
                    ),
                )

                self.conn.commit()

                trade_id = cursor.lastrowid

                self.logger.info(
                    f"💾 DATABASE | {status} saved | "
                    f"{data['symbol']} {data['side']} qty={data['qty']} id={trade_id}"
                )

                return int(trade_id) if trade_id else None

            except Exception as e:
                self.logger.error(f"❌ DB add_trade failed: {e}", exc_info=True)
                return None

    def get_last_trade(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Получение последней сделки по символу (для реализации cooldown логики).
        """
        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    "SELECT * FROM trades WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                    (symbol,),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
            except Exception as e:
                self.logger.error(f"❌ DB get_last_trade failed for {symbol}: {e}")
                return None

    def get_open_trade(
        self,
        symbol: str,
        side: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                cursor = self.conn.cursor()

                if side:
                    cursor.execute(
                        """
                        SELECT * FROM trades
                        WHERE symbol = ? AND side = ? AND status = 'OPEN'
                        ORDER BY entry_time DESC
                        LIMIT 1
                        """,
                        (symbol, side),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT * FROM trades
                        WHERE symbol = ? AND status = 'OPEN'
                        ORDER BY entry_time DESC
                        LIMIT 1
                        """,
                        (symbol,),
                    )

                row = cursor.fetchone()
                return dict(row) if row else None

            except Exception as e:
                self.logger.error(f"❌ DB get_open_trade failed for {symbol}: {e}")
                return None

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """
        Для восстановления состояния после рестарта.
        """
        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM trades
                    WHERE status = 'OPEN'
                    ORDER BY entry_time DESC
                    """
                )

                return [dict(row) for row in cursor.fetchall()]

            except Exception as e:
                self.logger.error(f"❌ DB get_open_positions failed: {e}")
                return []

    def get_pending_orders(self) -> List[Dict[str, Any]]:
        """Pending limit orders that are not confirmed as exchange positions yet."""
        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM trades
                    WHERE status = 'PENDING_ORDER'
                    ORDER BY entry_time DESC
                    """
                )
                return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                self.logger.error(f"❌ DB get_pending_orders failed: {e}")
                return []

    def mark_trade_open(
        self,
        symbol: str,
        side: Optional[str] = None,
        order_id: Optional[str] = None,
        entry_price: Optional[float] = None,
        qty: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> bool:
        """Promote a pending order to an open position after exchange fill is visible."""
        with self._lock:
            try:
                updates = ["status = 'OPEN'"]
                params: List[Any] = []

                if entry_price is not None and float(entry_price) > 0:
                    updates.append("entry_price = ?")
                    params.append(float(entry_price))

                if qty is not None and float(qty) > 0:
                    updates.append("qty = ?")
                    params.append(float(qty))

                if stop_loss is not None and float(stop_loss) > 0:
                    updates.append("stop_loss = ?")
                    params.append(float(stop_loss))

                if order_id:
                    where = "order_id = ? AND status = 'PENDING_ORDER'"
                    params.append(str(order_id))
                elif side:
                    where = """
                        id = (
                            SELECT id FROM trades
                            WHERE symbol = ? AND side = ? AND status = 'PENDING_ORDER'
                            ORDER BY entry_time DESC
                            LIMIT 1
                        )
                    """
                    params.extend([symbol, side])
                else:
                    where = """
                        id = (
                            SELECT id FROM trades
                            WHERE symbol = ? AND status = 'PENDING_ORDER'
                            ORDER BY entry_time DESC
                            LIMIT 1
                        )
                    """
                    params.append(symbol)

                query = f"UPDATE trades SET {', '.join(updates)} WHERE {where}"
                cursor = self.conn.cursor()
                cursor.execute(query, params)
                self.conn.commit()

                if cursor.rowcount <= 0:
                    return False

                self.logger.info(f"💾 DATABASE | PENDING -> OPEN | {symbol}")
                return True
            except Exception as e:
                self.logger.error(f"❌ DB mark_trade_open failed for {symbol}: {e}", exc_info=True)
                return False

    def mark_trade_cancelled(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        trade_id: Optional[int] = None,
    ) -> bool:
        """Mark a pending order as cancelled without polluting realised PnL."""
        with self._lock:
            try:
                now = datetime.now(timezone.utc).isoformat()

                if trade_id is not None:
                    query = """
                        UPDATE trades
                        SET exit_time = ?, status = 'CANCELLED'
                        WHERE id = ? AND status = 'PENDING_ORDER'
                    """
                    params = (now, int(trade_id))
                elif order_id:
                    query = """
                        UPDATE trades
                        SET exit_time = ?, status = 'CANCELLED'
                        WHERE order_id = ? AND status = 'PENDING_ORDER'
                    """
                    params = (now, str(order_id))
                else:
                    query = """
                        UPDATE trades
                        SET exit_time = ?, status = 'CANCELLED'
                        WHERE id = (
                            SELECT id FROM trades
                            WHERE symbol = ? AND status = 'PENDING_ORDER'
                            ORDER BY entry_time DESC
                            LIMIT 1
                        )
                    """
                    params = (now, symbol)

                cursor = self.conn.cursor()
                cursor.execute(query, params)
                self.conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                self.logger.error(f"❌ DB mark_trade_cancelled failed for {symbol}: {e}", exc_info=True)
                return False

    def close_trade(
        self,
        symbol: str,
        exit_price: float,
        pnl_usd: float,
        pnl_pct: float,
        order_id: Optional[str] = None,
        trade_id: Optional[int] = None,
        side: Optional[str] = None,
        status: str = "CLOSED",
    ) -> bool:
        """
        Закрытие позиции.
        Приоритет:
        1. trade_id
        2. order_id
        3. symbol + side
        4. symbol
        """
        with self._lock:
            try:
                now = datetime.now(timezone.utc).isoformat()
                close_status = str(status).upper()
                if close_status not in {"CLOSED", "CLOSED_UNVERIFIED"}:
                    self.logger.warning(
                        f"⚠️ DB close_trade: invalid close status {close_status}, falling back to CLOSED"
                    )
                    close_status = "CLOSED"

                if trade_id is not None:
                    query = """
                        UPDATE trades
                        SET exit_time = ?, exit_price = ?, pnl_usd = ?, pnl_pct = ?, status = ?
                        WHERE id = ? AND status = 'OPEN'
                    """
                    params = (now, float(exit_price), float(pnl_usd), float(pnl_pct), close_status, int(trade_id))

                elif order_id:
                    query = """
                        UPDATE trades
                        SET exit_time = ?, exit_price = ?, pnl_usd = ?, pnl_pct = ?, status = ?
                        WHERE order_id = ? AND status = 'OPEN'
                    """
                    params = (now, float(exit_price), float(pnl_usd), float(pnl_pct), close_status, str(order_id))

                elif side:
                    query = """
                        UPDATE trades
                        SET exit_time = ?, exit_price = ?, pnl_usd = ?, pnl_pct = ?, status = ?
                        WHERE id = (
                            SELECT id FROM trades
                            WHERE symbol = ? AND side = ? AND status = 'OPEN'
                            ORDER BY entry_time DESC
                            LIMIT 1
                        )
                    """
                    params = (now, float(exit_price), float(pnl_usd), float(pnl_pct), close_status, symbol, side)

                else:
                    query = """
                        UPDATE trades
                        SET exit_time = ?, exit_price = ?, pnl_usd = ?, pnl_pct = ?, status = ?
                        WHERE id = (
                            SELECT id FROM trades
                            WHERE symbol = ? AND status = 'OPEN'
                            ORDER BY entry_time DESC
                            LIMIT 1
                        )
                    """
                    params = (now, float(exit_price), float(pnl_usd), float(pnl_pct), close_status, symbol)

                cursor = self.conn.cursor()
                cursor.execute(query, params)
                self.conn.commit()

                if cursor.rowcount <= 0:
                    self.logger.warning(f"⚠️ DB close_trade: no OPEN trade matched for {symbol}")
                    return False

                self.logger.info(
                    f"💾 DATABASE | {close_status} saved | {symbol} PnL={pnl_usd:.2f} USD"
                )
                return True

            except Exception as e:
                self.logger.error(f"❌ DB close_trade failed for {symbol}: {e}", exc_info=True)
                return False

    def get_today_pnl_usd(self) -> float:
        with self._lock:
            try:
                now_utc = datetime.now(timezone.utc)
                start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                end = now_utc.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()

                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(pnl_usd), 0.0)
                    FROM trades
                    WHERE status = 'CLOSED'
                      AND exit_time >= ?
                      AND exit_time <= ?
                    """,
                    (start, end),
                )

                return float(cursor.fetchone()[0] or 0.0)

            except Exception as e:
                self.logger.error(f"❌ DB get_today_pnl_usd failed: {e}")
                return 0.0

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            try:
                cursor = self.conn.cursor()
                cursor.execute(
                    """
                    SELECT pnl_usd FROM trades
                    WHERE status = 'CLOSED'
                    """
                )

                pnls = [float(row[0] or 0.0) for row in cursor.fetchall()]

                if not pnls:
                    return {
                        "total_trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "winrate": 0.0,
                        "profit_factor": 0.0,
                        "net_pnl": 0.0,
                    }

                wins = [v for v in pnls if v > 0]
                losses = [v for v in pnls if v < 0]

                gross_profit = sum(wins)
                gross_loss = abs(sum(losses))

                return {
                    "total_trades": len(pnls),
                    "wins": len(wins),
                    "losses": len(losses),
                    "winrate": round(len(wins) / len(pnls) * 100, 2),
                    "profit_factor": round(gross_profit / gross_loss, 2)
                    if gross_loss > 0
                    else round(gross_profit, 2),
                    "net_pnl": round(sum(pnls), 2),
                }

            except Exception as e:
                self.logger.error(f"❌ DB get_stats failed: {e}")
                return {}

    def save_scan_result(
        self,
        symbol: str,
        trend: str,
        score: int,
        details: str,
    ) -> bool:
        with self._lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO scan_history (ts, symbol, trend, score, details)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        str(symbol),
                        str(trend),
                        int(score),
                        str(details),
                    ),
                )
                self.conn.commit()
                return True

            except Exception as e:
                self.logger.debug(f"DB save_scan_result failed: {e}")
                return False

    def maintenance(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()

        with self._lock:
            try:
                self.conn.execute(
                    "DELETE FROM scan_history WHERE ts < ?",
                    (cutoff,),
                )
                self.conn.commit()
                self.logger.info("🧹 Database scan_history cleanup completed")

            except Exception as e:
                self.logger.error(f"❌ DB maintenance cleanup failed: {e}")
                return

            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self.conn.execute("VACUUM;")
                self.logger.info("💎 Database optimization completed")

            except Exception as e:
                self.logger.error(f"❌ DB VACUUM failed: {e}")

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
                self.logger.info("DB connection closed")
            except Exception as e:
                self.logger.error(f"DB close failed: {e}")
