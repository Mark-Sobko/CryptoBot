import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
from core.database import TradeDatabase
from core.database_sync import DatabaseSync
from core.executor import TradeExecutor
from core.paper_trading import PaperExchangeManager


def _remove_db_files(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def _wire_executor_to_db(executor: TradeExecutor, db_path: Path) -> None:
    db_sync = DatabaseSync(db_path=str(db_path))
    executor.database_sync = db_sync
    executor.position_manager.database_sync = db_sync
    executor.request_delay = 0
    executor.tp_manager.request_delay = 0
    executor.position_manager.tp_manager.request_delay = 0


def run_paper_lifecycle(
    db_path: Path,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    entry_price: float = 100.0,
    stop_loss: float = 95.0,
    close_price: float = 106.0,
    qty: float = 1.0,
    score: int = 90,
    reset_db: bool = False,
) -> Dict[str, Any]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if reset_db and db_path.exists():
        _remove_db_files(db_path)

    exchange = PaperExchangeManager(symbol=symbol, last_price=entry_price)
    executor = TradeExecutor(exchange)
    _wire_executor_to_db(executor, db_path)

    db = TradeDatabase(str(db_path))
    try:
        poi = {
            "type": "PAPER_OB",
            "side": side.upper(),
            "top": entry_price + 1.0,
            "bottom": entry_price - 1.0,
            "mid": entry_price,
        }

        response = executor.execute_institutional_entry(
            symbol=symbol,
            side=side,
            poi=poi,
            score=score,
            qty=qty,
            sl=stop_loss,
            risk_pct=1.0,
            order_type="Limit",
            limit_price=entry_price,
        )

        if not response:
            raise RuntimeError("Paper entry rejected by executor")

        order_id = str(response.get("result", {}).get("orderId", ""))
        pending_after_entry = len(db.get_pending_orders())

        filled_position = exchange.session.fill_order(order_id)
        if not filled_position:
            raise RuntimeError(f"Paper fill failed for order {order_id}")

        exchange.sync_db_with_exchange(db)
        open_after_fill = len(db.get_open_positions())

        active_positions = exchange.get_active_positions()
        if not active_positions:
            raise RuntimeError("Paper fill did not create an active position")

        executor.manage_position_pro(active_positions[0])
        open_orders_after_tp = exchange.session.get_open_orders(
            category=exchange.CATEGORY,
            symbol=symbol,
        )["result"]["list"]
        reduce_only_tp_orders = [
            order
            for order in open_orders_after_tp
            if str(order.get("reduceOnly", "")).lower() == "true"
        ]

        closed_pnl = exchange.session.close_position(symbol, close_price)
        if not closed_pnl:
            raise RuntimeError("Paper close failed")

        exchange.sync_db_with_exchange(db)
        stats = db.get_stats()

        return {
            "status": "OK",
            "db_path": str(db_path),
            "symbol": symbol,
            "side": side.upper(),
            "entry_order_id": order_id,
            "pending_after_entry": pending_after_entry,
            "open_after_fill": open_after_fill,
            "tp_orders_after_manage": len(reduce_only_tp_orders),
            "closed_pnl": closed_pnl,
            "stats": stats,
        }
    finally:
        db.close()


def _reduce_only_orders(exchange: PaperExchangeManager, symbol: str) -> list[dict[str, Any]]:
    orders = exchange.session.get_open_orders(
        category=exchange.CATEGORY,
        symbol=symbol,
    )["result"]["list"]
    return [order for order in orders if str(order.get("reduceOnly", "")).lower() == "true"]


def _sum_order_qty(orders: list[dict[str, Any]]) -> float:
    return round(sum(float(order.get("qty", 0.0) or 0.0) for order in orders), 8)


def run_paper_partial_fill_recovery(
    db_path: Path,
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    entry_price: float = 100.0,
    stop_loss: float = 95.0,
    close_price: float = 106.0,
    qty: float = 1.0,
    partial_qty: float = 0.4,
    score: int = 90,
    reset_db: bool = False,
) -> Dict[str, Any]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if reset_db:
        _remove_db_files(db_path)

    exchange = PaperExchangeManager(symbol=symbol, last_price=entry_price)
    executor = TradeExecutor(exchange)
    _wire_executor_to_db(executor, db_path)

    db = TradeDatabase(str(db_path))
    try:
        poi = {
            "type": "PAPER_PARTIAL_FILL_OB",
            "side": side.upper(),
            "top": entry_price + 1.0,
            "bottom": entry_price - 1.0,
            "mid": entry_price,
        }

        response = executor.execute_institutional_entry(
            symbol=symbol,
            side=side,
            poi=poi,
            score=score,
            qty=qty,
            sl=stop_loss,
            risk_pct=1.0,
            order_type="Limit",
            limit_price=entry_price,
        )

        if not response:
            raise RuntimeError("Paper partial-fill entry rejected by executor")

        order_id = str(response.get("result", {}).get("orderId", ""))
        pending_after_entry = len(db.get_pending_orders())

        if not exchange.session.partially_fill_order(order_id, partial_qty):
            raise RuntimeError(f"Paper partial fill failed for order {order_id}")

        exchange.sync_db_with_exchange(db)
        open_trade_after_partial = db.get_open_trade(symbol=symbol, side=side.upper())
        if not open_trade_after_partial:
            raise RuntimeError("Partial fill did not promote DB trade to OPEN")

        pending_exchange_after_partial = exchange.get_pending_entry_orders(symbol) or []
        active_positions = exchange.get_active_positions()
        if not active_positions:
            raise RuntimeError("Partial fill did not create an active position")

        executor.manage_position_pro(active_positions[0])
        partial_tp_orders = _reduce_only_orders(exchange, symbol)

        restarted_executor = TradeExecutor(exchange)
        _wire_executor_to_db(restarted_executor, db_path)
        restarted_executor.manage_position_pro(exchange.get_active_positions()[0])
        restarted_tp_orders = _reduce_only_orders(exchange, symbol)

        if not exchange.session.fill_order(order_id):
            raise RuntimeError(f"Paper final fill failed for order {order_id}")

        exchange.sync_db_with_exchange(db)
        open_trade_after_full = db.get_open_trade(symbol=symbol, side=side.upper())
        if not open_trade_after_full:
            raise RuntimeError("Full fill lost DB OPEN trade")

        restarted_executor.manage_position_pro(exchange.get_active_positions()[0])
        full_tp_orders = _reduce_only_orders(exchange, symbol)

        closed_pnl = exchange.session.close_position(symbol, close_price)
        if not closed_pnl:
            raise RuntimeError("Paper close failed")

        exchange.sync_db_with_exchange(db)
        stats = db.get_stats()

        return {
            "status": "OK",
            "db_path": str(db_path),
            "symbol": symbol,
            "side": side.upper(),
            "entry_order_id": order_id,
            "pending_after_entry": pending_after_entry,
            "db_qty_after_partial": float(open_trade_after_partial.get("qty", 0.0) or 0.0),
            "exchange_pending_after_partial": len(pending_exchange_after_partial),
            "partial_tp_order_count": len(partial_tp_orders),
            "partial_tp_qty_sum": _sum_order_qty(partial_tp_orders),
            "restart_tp_order_count": len(restarted_tp_orders),
            "restart_tp_qty_sum": _sum_order_qty(restarted_tp_orders),
            "db_qty_after_full": float(open_trade_after_full.get("qty", 0.0) or 0.0),
            "full_tp_order_count": len(full_tp_orders),
            "full_tp_qty_sum": _sum_order_qty(full_tp_orders),
            "closed_pnl": closed_pnl,
            "stats": stats,
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a local paper lifecycle: pending limit -> fill -> TP cascade -> close -> stats."
    )
    parser.add_argument("--db", type=Path, default=Path(config.DATA_DIR) / "paper_lifecycle.db")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--side", choices=["LONG", "SHORT"], default="LONG")
    parser.add_argument("--entry", type=float, default=100.0)
    parser.add_argument("--stop", type=float, default=95.0)
    parser.add_argument("--close", type=float, default=106.0)
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--partial-fill-qty", type=float, default=0.4)
    parser.add_argument("--score", type=int, default=90)
    parser.add_argument("--reset-db", action="store_true")
    parser.add_argument("--partial-fill-recovery", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

    if args.partial_fill_recovery:
        result = run_paper_partial_fill_recovery(
            db_path=args.db,
            symbol=args.symbol,
            side=args.side,
            entry_price=args.entry,
            stop_loss=args.stop,
            close_price=args.close,
            qty=args.qty,
            partial_qty=args.partial_fill_qty,
            score=args.score,
            reset_db=args.reset_db,
        )
    else:
        result = run_paper_lifecycle(
            db_path=args.db,
            symbol=args.symbol,
            side=args.side,
            entry_price=args.entry,
            stop_loss=args.stop,
            close_price=args.close,
            qty=args.qty,
            score=args.score,
            reset_db=args.reset_db,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
