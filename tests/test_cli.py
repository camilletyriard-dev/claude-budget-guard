import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from claude_budget.cli import main


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"CLAUDE_BUDGET_DB": str(Path(self.tmp.name) / "l.db")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("ANTHROPIC_ADMIN_KEY", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_set_add_status_history(self):
        self.cli("set", "rare-cancer", "200")
        self.cli("add", "rare-cancer", "12.5", "--note", "batch job")
        code, out = self.cli("status")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"rare-cancer\s+\$200\.00\s+\$12\.5000\s+\$187\.50")
        _, out = self.cli("history", "rare-cancer")
        self.assertIn("batch job", out)
        self.cli("set", "rare-cancer", "none")
        _, out = self.cli("status")
        self.assertRegex(out, r"rare-cancer\s+-\s+\$12\.5000\s+-")

    def test_watchdog_needs_admin_key(self):
        code, out = self.cli("watchdog", "--caps", "missing.toml")
        self.assertEqual(code, 2)
        self.assertIn("ANTHROPIC_ADMIN_KEY", out)


if __name__ == "__main__":
    unittest.main()
