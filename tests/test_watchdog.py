import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from claude_budget.pricing import PRICES
from claude_budget.watchdog import AdminAPI, Config, row_cost, run

from .fakes import FakeAdmin, usage_row

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
KEYS = [{"id": "k1", "name": "camille-rare-cancer"}, {"id": "k2", "name": "camille-scratch"},
        {"id": "k3", "name": "kit-evolution"}]


def opus_usd(usd):
    """A usage row for key costing `usd` at the Broad rate (Opus 5.5 output: $10 per million)."""
    return lambda key: usage_row(key, "claude-opus-5-5", output=int(usd * 100_000))


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "state.json")
        self.messages = []

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, rows, enforce=False, summary=False, **cfg):
        cfg = Config(price_multiplier=0.5, start="2026-10-01", state_file=self.state, **cfg)
        self.admin = getattr(self, "admin", None) or FakeAdmin(KEYS, rows)
        self.admin.rows = rows
        api = AdminAPI("sk-ant-admin01-test")
        api.request = self.admin.request
        out = io.StringIO()
        targets = run(cfg, api, enforce=enforce, now=NOW, notify=self.messages.append, out=out, summary=summary)
        return {t.label: t for t in targets}, out.getvalue()

    def test_spend_per_key_and_pagination(self):
        rows = [opus_usd(30)("k1"), opus_usd(20)("k1"), opus_usd(5)("k3"), opus_usd(1)("k3")]
        t, _ = self.go(rows, keys={"camille-rare-cancer": 200, "kit-evolution": 100})
        self.assertAlmostEqual(t["key camille-rare-cancer"].spent, 50)
        self.assertAlmostEqual(t["key kit-evolution"].spent, 6)
        usage_calls = [r for r in self.admin.requests if r[1] == "/usage_report/messages"]
        self.assertEqual(len(usage_calls), 2)
        params = usage_calls[0][2]
        self.assertIn(("starting_at", "2026-10-01T00:00:00Z"), params)
        self.assertIn(("group_by[]", "api_key_id"), params)

    def test_dry_run_reports_but_does_not_disable(self):
        t, out = self.go([opus_usd(90)("k3")], keys={"kit-evolution": 100})
        self.assertIn("would disable kit-evolution", out)
        self.assertEqual(self.admin.updates, [])
        self.assertIn("(dry run)", self.messages[0])

    def test_enforce_disables_only_over_cap_keys(self):
        rows = [opus_usd(90)("k3"), opus_usd(10)("k1")]
        self.go(rows, enforce=True, keys={"kit-evolution": 100, "camille-rare-cancer": 200})
        self.assertEqual(self.admin.updates, [("k3", "inactive")])
        self.assertIn("Disabled: kit-evolution", self.messages[-1])

    def test_alerts_once_per_level(self):
        caps = {"camille-rare-cancer": 100}
        self.go([opus_usd(55)("k1")], keys=caps)
        self.go([opus_usd(60)("k1")], keys=caps)  # still 50-80%: no new message
        self.assertEqual(len(self.messages), 1)
        self.assertIn("at 55%", self.messages[0])
        self.go([opus_usd(81)("k1")], keys=caps)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("at 81%", self.messages[1])

    def test_person_cap_sums_keys_and_disables_all(self):
        rows = [opus_usd(50)("k1"), opus_usd(40)("k2"), opus_usd(80)("k3")]
        t, _ = self.go(rows, enforce=True, people={"camille": 100})
        self.assertAlmostEqual(t["person camille"].spent, 90)
        self.assertEqual(sorted(self.admin.updates), [("k1", "inactive"), ("k2", "inactive")])

    def test_reenabled_key_without_new_cap_goes_off_again(self):
        caps = {"kit-evolution": 100}
        self.go([opus_usd(90)("k3")], enforce=True, keys=caps)
        self.admin.keys[2]["status"] = "active"  # someone re-enables it in Console
        self.go([opus_usd(90)("k3")], enforce=True, keys=caps)
        self.assertEqual(self.admin.updates, [("k3", "inactive")] * 2)

    def test_default_cap_and_unlisted_usage(self):
        t, out = self.go([opus_usd(5)("k2"), opus_usd(3)(None)], default_key_cap=10)
        self.assertEqual(len(t), 3)
        self.assertAlmostEqual(t["key camille-scratch"].spent, 5)
        self.assertIn("Console playground", out)

    def test_daily_overview(self):
        rows = [opus_usd(30)("k1"), opus_usd(20)("k2"), opus_usd(80)("k3"),
                dict(opus_usd(100)("k3"), day="2026-10-20"), opus_usd(7)(None)]
        self.go(rows, summary=True, total_budget=1000, people={"camille": 400},
                keys={"kit-evolution": 400})
        self.assertEqual(len(self.messages), 1)  # nothing near a cap: only the overview
        text = self.messages[0]
        self.assertIn("since 2026-10-01", text)
        self.assertIn("Total $237.00 of $1,000.00 (24%) · yesterday $137.00", text)
        self.assertIn("By person: *kit* $180.00 · *camille* $50.00 of $400.00 (12%) · *playground", text)
        self.assertIn("*kit-evolution* $180.00 of $400.00 (45%) · *camille-rare-cancer* $30.00", text)

    def test_no_overview_unless_asked(self):
        self.go([opus_usd(30)("k1")], keys={"camille-rare-cancer": 1000})
        self.assertEqual(self.messages, [])

    def test_row_cost_rules(self):
        self.assertAlmostEqual(row_cost(usage_row("k", "claude-opus-5-5", output=1_000_000, tier="batch"), 1.0), 10)
        unknown = row_cost(usage_row("k", "claude-new-model", output=1_000_000), 1.0)
        self.assertAlmostEqual(unknown, max(o for _, o, _ in PRICES.values()))  # most expensive model

    def test_monthly_period_and_config_file(self):
        path = Path(self.tmp.name) / "caps.toml"
        path.write_text('period = "monthly"\nprice_multiplier = 0.5\n[keys]\n"kit-evolution" = 100\n'
                        '[people]\ncamille = 400\n')
        cfg = Config.load(path)
        self.assertEqual(cfg.keys, {"kit-evolution": 100})
        self.assertEqual(cfg.people, {"camille": 400})
        self.assertEqual(cfg.period_start(NOW), datetime(2026, 10, 1, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            Config(period="weekly").period_start(NOW)


if __name__ == "__main__":
    unittest.main()
