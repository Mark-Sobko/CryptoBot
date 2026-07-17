import argparse
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


DEFAULT_SYMBOLS = ["SUIUSDT", "INJUSDT", "WIFUSDT", "TIAUSDT"]


def _default_db_path() -> Path:
    try:
        import config

        return Path(config.DB_PATH)
    except Exception:
        return Path(__file__).resolve().parent / "data" / "bot_memory.db"


def _build_query(symbols: Iterable[str], limit: int) -> tuple[str, List[object]]:
    symbol_list = [str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()]
    placeholders = ",".join("?" for _ in symbol_list)
    where_clause = f"WHERE symbol IN ({placeholders})" if symbol_list else ""

    query = f"""
        SELECT symbol, side, entry_price, stop_loss, entry_time, pnl_usd, status
        FROM trades
        {where_clause}
        ORDER BY entry_time DESC
        LIMIT ?
    """
    return query, [*symbol_list, int(limit)]


def _fetch_rows(db_path: Path, query: str, params: Sequence[object]) -> List[Dict[str, object]]:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def _print_rows(rows: Sequence[Dict[str, object]]) -> None:
    if not rows:
        return

    columns = ["symbol", "side", "entry_price", "stop_loss", "entry_time", "pnl_usd", "status"]
    widths = {
        col: max(len(col), *(len(str(row.get(col, ""))) for row in rows[:10]))
        for col in columns
    }

    header = " | ".join(col.ljust(widths[col]) for col in columns)
    print(header)
    print("-" * len(header))

    for row in rows[:10]:
        print(" | ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns))


def analyze_trades(db_path: Path, symbols: Iterable[str], limit: int) -> int:
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        return 1

    query, params = _build_query(symbols, limit)
    rows = _fetch_rows(db_path, query, params)

    print("--- Последние сделки по выбранным монетам ---")
    if not rows:
        print("Нет сделок для выбранного фильтра.")
        return 0

    _print_rows(rows)

    print("\n--- Институциональная статистика ---")
    closed_trades = [row for row in rows if row.get("status") == "CLOSED"]
    if not closed_trades:
        print("Нет подтвержденно закрытых сделок для анализа статистики.")
        return 0

    pnl_values = [float(row.get("pnl_usd", 0.0) or 0.0) for row in closed_trades]
    wins = len([pnl for pnl in pnl_values if pnl > 0])
    total = len(closed_trades)
    winrate = (wins / total) * 100

    print(f"Кол-во закрытых сделок: {total}")
    print(f"WinRate: {winrate:.2f}%")
    print(f"Суммарный PnL: {sum(pnl_values):.2f} USD")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze recent CryptoBot trades from SQLite.")
    parser.add_argument("--db", type=Path, default=_default_db_path(), help="Path to bot_memory.db")
    parser.add_argument("--limit", type=int, default=20, help="Maximum trades to load")
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS, help="Optional symbols filter")
    args = parser.parse_args()

    return analyze_trades(args.db, args.symbols, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
