import unittest

from scripts.secret_scan import is_blocked_runtime_path, scan_blob_text


class SecretScanTests(unittest.TestCase):
    def test_blocks_runtime_and_secret_paths(self):
        blocked = [
            ".env",
            ".env.local",
            "data/bot_memory.db",
            "data/trade_history.json",
            "logs/bot_execution.log",
            "core/__pycache__/executor.pyc",
            "core/.DS_Store",
            "deploy_log.txt",
        ]

        for path in blocked:
            with self.subTest(path=path):
                self.assertTrue(is_blocked_runtime_path(path))

    def test_allows_safe_paths(self):
        allowed = [
            ".env.example",
            "core/database.py",
            "requirements-lock.txt",
            "tests/test_secret_scan.py",
        ]

        for path in allowed:
            with self.subTest(path=path):
                self.assertFalse(is_blocked_runtime_path(path))

    def test_detects_non_empty_secret_assignment(self):
        sample = "BYBIT_API_SECRET" + "=real-value\n"
        findings = scan_blob_text(sample, "sample")

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].detail, "BYBIT_API_SECRET")

    def test_ignores_empty_env_example_assignment(self):
        findings = scan_blob_text("BYBIT_API_KEY=\nTELEGRAM_TOKEN=\n", ".env.example")

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
