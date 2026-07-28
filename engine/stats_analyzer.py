import html
import logging
import sqlite3
from typing import List

import numpy as np
import pandas as pd
from tabulate import tabulate

import config


class StatsAnalyzer:
    """
    [INSTITUTIONAL STATS ANALYZER v5.0]
    Аналитика только по CLOSED-сделкам из SQLite v5.0.
    """

    def __init__(self, db_path: str = config.DB_PATH, initial_balance: float = 1000.0):
        self.db_path = str(db_path)
        self.initial_balance = float(initial_balance)
        self.logger = logging.getLogger("SMC_BOT.StatsAnalyzer")

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _table_columns(conn, table: str) -> set[str]:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return {str(row["name"]) for row in cursor.fetchall()}

    def generate_report_chunks(self) -> List[str]:
        try:
            with self._get_connection() as conn:
                columns = self._table_columns(conn, "trades")
                strategy_select = (
                    "strategy"
                    if "strategy" in columns
                    else "'SMC' AS strategy"
                )
                source_select = (
                    "source"
                    if "source" in columns
                    else "'SMC' AS source"
                )

                query = f"""
                SELECT
                    id,
                    symbol,
                    side,
                    entry_time,
                    exit_time,
                    entry_price,
                    exit_price,
                    qty,
                    pnl_usd,
                    pnl_pct,
                    score,
                    poi_type,
                    {strategy_select},
                    {source_select},
                    rr
                FROM trades
                WHERE status = 'CLOSED'
                ORDER BY exit_time ASC
                """
                df = pd.read_sql_query(query, conn)

            if df.empty:
                return ["📉 [STATS] Нет закрытых сделок для генерации отчета."]

            df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, errors="coerce")
            df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True, errors="coerce")
            df["pnl_usd"] = pd.to_numeric(df["pnl_usd"], errors="coerce").fillna(0.0)
            df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0.0)
            df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0).astype(int)
            df["strategy"] = df["strategy"].fillna("SMC").astype(str).replace("", "SMC")

            df = df.dropna(subset=["exit_time"]).sort_values("exit_time").reset_index(drop=True)

            if df.empty:
                return ["📉 [STATS] Нет валидных закрытых сделок для отчета."]

            chunks: List[str] = []

            total_trades = len(df)
            wins = df[df["pnl_usd"] > 0]
            losses = df[df["pnl_usd"] < 0]
            breakevens = df[df["pnl_usd"] == 0]

            winrate = len(wins) / total_trades * 100 if total_trades else 0.0
            total_pnl = float(df["pnl_usd"].sum())
            final_balance = self.initial_balance + total_pnl

            balance_curve = self.initial_balance + df["pnl_usd"].cumsum()
            peaks = balance_curve.cummax()
            drawdowns = peaks - balance_curve
            max_abs_dd = float(drawdowns.max()) if not drawdowns.empty else 0.0
            max_dd_pct = max_abs_dd / self.initial_balance * 100 if self.initial_balance > 0 else 0.0

            gross_profit = float(wins["pnl_usd"].sum())
            gross_loss = float(abs(losses["pnl_usd"].sum()))

            if gross_loss == 0:
                profit_factor = "∞" if gross_profit > 0 else "0.00"
            else:
                profit_factor = f"{gross_profit / gross_loss:.2f}"

            core_metrics = [
                ["Всего закрытых сделок", total_trades],
                ["Победы / Убытки / БУ", f"{len(wins)} / {len(losses)} / {len(breakevens)}"],
                ["Win Rate", f"{winrate:.1f}%"],
                ["Gross Profit", f"{gross_profit:.2f} USDT"],
                ["Gross Loss", f"{gross_loss:.2f} USDT"],
                ["Profit Factor", profit_factor],
                ["Max Drawdown", f"{max_dd_pct:.2f}% ({max_abs_dd:.2f} USDT)"],
                ["Net PnL", f"{total_pnl:+.2f} USDT"],
                ["Final Balance", f"{final_balance:.2f} USDT"],
            ]

            table_1 = html.escape(tabulate(core_metrics, tablefmt="plain"))
            chunks.append(
                "📊 <b>ИНСТИТУЦИОНАЛЬНЫЙ АУДИТ СТАТИСТИКИ</b>\n"
                "----------------------------------------\n"
                f"<pre>{table_1}</pre>"
            )

            states = np.where(df["pnl_usd"] > 0, 1, np.where(df["pnl_usd"] < 0, -1, 0))

            max_win_run = 0
            max_loss_run = 0
            current_win_run = 0
            current_loss_run = 0

            for state in states:
                if state == 1:
                    current_win_run += 1
                    max_loss_run = max(max_loss_run, current_loss_run)
                    current_loss_run = 0
                elif state == -1:
                    current_loss_run += 1
                    max_win_run = max(max_win_run, current_win_run)
                    current_win_run = 0
                else:
                    max_win_run = max(max_win_run, current_win_run)
                    max_loss_run = max(max_loss_run, current_loss_run)
                    current_win_run = 0
                    current_loss_run = 0

            max_win_run = max(max_win_run, current_win_run)
            max_loss_run = max(max_loss_run, current_loss_run)

            avg_win = float(wins["pnl_usd"].mean()) if not wins.empty else 0.0
            avg_loss = float(abs(losses["pnl_usd"].mean())) if not losses.empty else 0.0
            realized_rr = avg_win / avg_loss if avg_loss > 0 else 0.0
            expectancy = total_pnl / total_trades if total_trades else 0.0

            risk_stats = [
                ["Avg Win", f"{avg_win:.2f} USDT"],
                ["Avg Loss", f"{avg_loss:.2f} USDT"],
                ["Realized R:R", f"1:{realized_rr:.2f}"],
                ["Expectancy / Trade", f"{expectancy:+.2f} USDT"],
                ["Max Win Streak", max_win_run],
                ["Max Loss Streak", max_loss_run],
            ]

            table_2 = html.escape(tabulate(risk_stats, tablefmt="plain"))
            chunks.append(
                "📈 <b>АНАЛИЗ СЕРИЙ И РИСКОВ</b>\n"
                "----------------------------------------\n"
                f"<pre>{table_2}</pre>"
            )

            asset_df = (
                df.groupby("symbol")
                .agg(
                    Trades=("id", "count"),
                    PnL=("pnl_usd", "sum"),
                    Winrate=("pnl_usd", lambda x: (x > 0).sum() / len(x) * 100),
                )
                .sort_values("PnL", ascending=False)
                .reset_index()
            )

            asset_df["symbol"] = asset_df["symbol"].astype(str).str.replace("USDT", "", regex=False)

            table_3 = tabulate(
                asset_df.values,
                headers=["Пара", "Кол-во", "PnL", "Win%"],
                tablefmt="simple",
                floatfmt=("", ".0f", ".2f", ".1f"),
            )

            chunks.append(
                "📊 <b>РЕЗУЛЬТАТИВНОСТЬ ИНСТРУМЕНТОВ</b>\n"
                "----------------------------------------\n"
                f"<pre>{html.escape(table_3)}</pre>"
            )

            strategy_rows = []
            for strategy, group in df.groupby("strategy"):
                group_wins = group[group["pnl_usd"] > 0]
                group_losses = group[group["pnl_usd"] < 0]
                group_gross_profit = float(group_wins["pnl_usd"].sum())
                group_gross_loss = float(abs(group_losses["pnl_usd"].sum()))
                if group_gross_loss == 0:
                    group_profit_factor = "∞" if group_gross_profit > 0 else "0.00"
                else:
                    group_profit_factor = f"{group_gross_profit / group_gross_loss:.2f}"

                strategy_rows.append(
                    [
                        strategy,
                        len(group),
                        float(group["pnl_usd"].sum()),
                        (len(group_wins) / len(group) * 100) if len(group) else 0.0,
                        group_profit_factor,
                    ]
                )

            strategy_rows.sort(key=lambda row: row[2], reverse=True)
            table_4 = tabulate(
                strategy_rows,
                headers=["Стратегия", "Сделки", "PnL", "Win%", "PF"],
                tablefmt="simple",
                floatfmt=("", ".0f", ".2f", ".1f", ""),
            )

            chunks.append(
                "📊 <b>РЕЗУЛЬТАТИВНОСТЬ СТРАТЕГИЙ</b>\n"
                "----------------------------------------\n"
                f"<pre>{html.escape(table_4)}</pre>"
            )

            return chunks

        except Exception as e:
            self.logger.error(f"StatsAnalyzer report failed: {e}", exc_info=True)
            return [f"❌ Ошибка генерации отчета статистики: {html.escape(str(e))}"]
