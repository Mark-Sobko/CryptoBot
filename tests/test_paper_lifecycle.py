import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.run_paper_lifecycle import run_paper_lifecycle


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


if __name__ == "__main__":
    unittest.main()
