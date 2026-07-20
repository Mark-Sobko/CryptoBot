import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.run_paper_lifecycle import run_paper_lifecycle, run_paper_partial_fill_recovery


class PaperLifecycleTests(unittest.TestCase):
    def test_paper_lifecycle_exercises_pending_fill_tp_and_close(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "paper_lifecycle.db"

            summary = run_paper_lifecycle(db_path=db_path, reset_db=True)

            self.assertEqual(summary["status"], "OK")
            self.assertEqual(summary["pending_after_entry"], 1)
            self.assertEqual(summary["open_after_fill"], 1)
            self.assertEqual(summary["tp_orders_after_manage"], 3)
            self.assertEqual(summary["stats"]["total_trades"], 1)
            self.assertEqual(summary["stats"]["wins"], 1)
            self.assertEqual(summary["stats"]["net_pnl"], 6.0)

            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT status, pnl_usd, exit_price FROM trades ORDER BY id DESC LIMIT 1"
                ).fetchone()

            self.assertEqual(row[0], "CLOSED")
            self.assertEqual(float(row[1]), 6.0)
            self.assertEqual(float(row[2]), 106.0)

    def test_paper_partial_fill_restart_recovery_refreshes_tp_qty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "paper_partial_lifecycle.db"

            summary = run_paper_partial_fill_recovery(
                db_path=db_path,
                reset_db=True,
                qty=1.0,
                partial_qty=0.4,
            )

            self.assertEqual(summary["status"], "OK")
            self.assertEqual(summary["pending_after_entry"], 1)
            self.assertEqual(summary["db_qty_after_partial"], 0.4)
            self.assertEqual(summary["exchange_pending_after_partial"], 1)
            self.assertEqual(summary["partial_tp_order_count"], 3)
            self.assertEqual(summary["partial_tp_qty_sum"], 0.4)
            self.assertEqual(summary["restart_tp_order_count"], 3)
            self.assertEqual(summary["restart_tp_qty_sum"], 0.4)
            self.assertEqual(summary["db_qty_after_full"], 1.0)
            self.assertEqual(summary["full_tp_order_count"], 3)
            self.assertEqual(summary["full_tp_qty_sum"], 1.0)
            self.assertEqual(summary["stats"]["total_trades"], 1)
            self.assertEqual(summary["stats"]["net_pnl"], 6.0)


if __name__ == "__main__":
    unittest.main()
