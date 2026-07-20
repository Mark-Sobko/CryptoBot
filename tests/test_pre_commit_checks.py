import unittest

from scripts.pre_commit_checks import existing_compile_targets, scan_staged_changes


class PreCommitChecksTests(unittest.TestCase):
    def test_staged_scan_blocks_runtime_paths_and_secret_patterns(self):
        secret_key = "BYBIT_API_KEY"
        findings = scan_staged_changes(
            [".env", "core/example.py"],
            lambda path: f"{secret_key}=not-a-real-key\n" if path.endswith(".py") else "",
        )

        locations = {finding.location for finding in findings}
        kinds = {finding.kind for finding in findings}

        self.assertIn(".env", locations)
        self.assertIn("staged:core/example.py", locations)
        self.assertIn("runtime-path", kinds)
        self.assertIn("secret-pattern", kinds)

    def test_compile_targets_exist(self):
        targets = existing_compile_targets()

        self.assertIn("core", targets)
        self.assertIn("tests", targets)
        self.assertIn("scripts", targets)


if __name__ == "__main__":
    unittest.main()
