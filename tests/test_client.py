import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace as NS

from claude_budget import BudgetedAnthropic, BudgetExceeded, CallCancelled, Ledger
from claude_budget.client import counts_from_usage
from claude_budget.pricing import Counts
from claude_budget.watchdog import row_cost

from .fakes import FakeClient, usage_row

MSG = [{"role": "user", "content": "hi"}]
SONNET = "claude-sonnet-5-5"  # $2 in / $10 out per million at list price


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "l.db")
        self.fake = FakeClient(input_tokens=1000, usage=dict(input_tokens=1000, output_tokens=200))
        self.posts = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kw):
        kw.setdefault("budget_usd", 1.0)
        kw.setdefault("notify", self.posts.append)
        return BudgetedAnthropic("p", ledger=self.ledger, client=self.fake, multiplier=1.0, verbose=False, **kw)

    # ---- estimates -------------------------------------------------------------
    def test_estimate_bounds(self):
        est = self.make().estimate(model=SONNET, max_tokens=500, messages=MSG)
        self.assertAlmostEqual(est.low_usd, 1000 * 2 / 1e6)
        self.assertAlmostEqual(est.high_usd, (1000 * 2 + 500 * 10) / 1e6)
        self.assertTrue(est.exact_input)
        self.assertEqual(set(self.fake.messages.count_calls[0]), {"model", "messages"})  # no max_tokens sent

    def test_estimate_with_caching_uses_write_price(self):
        sys_5m = [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]
        sys_1h = [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        c = self.make()
        self.assertAlmostEqual(c.estimate(model=SONNET, max_tokens=0, messages=MSG, system=sys_5m).high_usd,
                               1000 * 2 * 1.25 / 1e6)
        self.assertAlmostEqual(c.estimate(model=SONNET, max_tokens=0, messages=MSG, system=sys_1h).high_usd,
                               1000 * 2 * 2.0 / 1e6)

    def test_estimate_web_search_and_geo(self):
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
        est = self.make().estimate(model=SONNET, max_tokens=0, messages=MSG, tools=tools)
        self.assertAlmostEqual(est.high_usd, 1000 * 2 / 1e6 + 0.03)
        est = self.make().estimate(model=SONNET, max_tokens=0, messages=MSG, extra_body={"inference_geo": "us"})
        self.assertAlmostEqual(est.high_usd, 1000 * 2 * 1.1 / 1e6)

    def test_estimate_falls_back_when_count_fails(self):
        self.fake.messages.count_error = RuntimeError("429")
        est = self.make().estimate(model=SONNET, max_tokens=10, messages=MSG)
        self.assertFalse(est.exact_input)
        self.assertGreater(est.input_tokens, 0)

    # ---- the call --------------------------------------------------------------
    def test_create_records_actual_cost_and_prints(self):
        c = self.make()
        c.verbose = True
        err = io.StringIO()
        with redirect_stderr(err):
            resp = c.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        actual = (1000 * 2 + 200 * 10) / 1e6
        self.assertEqual(resp.id, "msg_1")
        self.assertAlmostEqual(self.ledger.spent("p"), actual)
        self.assertAlmostEqual(c.remaining(), 1.0 - actual)
        self.assertIn("estimate $0.0020 - $0.0070", err.getvalue())
        self.assertIn("actual $0.0040", err.getvalue())
        self.assertIn("remaining $1.00", err.getvalue())

    def test_over_budget_call_is_not_sent(self):
        c = self.make(budget_usd=0.005)
        with self.assertRaises(BudgetExceeded):
            c.messages.create(model=SONNET, max_tokens=1000, messages=MSG)  # worst case $0.012
        self.assertEqual(self.fake.messages.calls, [])
        self.assertEqual(self.ledger.spent("p"), 0)

    def test_budget_runs_out_over_several_calls(self):
        c = self.make(budget_usd=0.01)  # each call: worst $0.007, actual $0.004
        c.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        with self.assertRaises(BudgetExceeded):
            c.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        self.assertEqual(len(self.fake.messages.calls), 1)

    def test_api_error_releases_reservation(self):
        self.fake.messages.error = ConnectionError("down")
        with self.assertRaises(ConnectionError):
            self.make().messages.create(model=SONNET, max_tokens=500, messages=MSG)
        self.assertEqual(self.ledger.spent("p"), 0)

    def test_confirmation(self):
        asked = []
        no = self.make(confirm_above_usd=0.005, confirm=lambda m: asked.append(m) or False)
        with self.assertRaises(CallCancelled):
            no.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        self.assertEqual(self.fake.messages.calls, [])
        self.assertIn("worst case $0.0070, remaining $1.00. Continue?", asked[0])
        yes = self.make(confirm_above_usd=0.005, confirm=lambda m: True)
        yes.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        cheap = self.make(confirm_above_usd=1.0, confirm=lambda m: self.fail("should not ask"))
        cheap.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        self.assertEqual(len(self.fake.messages.calls), 2)

    def test_run_budget(self):
        c = self.make()
        with c.run_budget(0.01, name="loop") as run_id:
            c.messages.create(model=SONNET, max_tokens=500, messages=MSG)
            with self.assertRaises(BudgetExceeded):
                c.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        self.assertEqual(run_id, "loop")
        self.assertAlmostEqual(self.ledger.run_spent("loop"), 0.004)
        c.messages.create(model=SONNET, max_tokens=500, messages=MSG)  # outside the run: project budget only

    def test_streaming_is_refused(self):
        c = self.make()
        with self.assertRaises(NotImplementedError):
            c.messages.create(model=SONNET, max_tokens=5, messages=MSG, stream=True)
        with self.assertRaises(NotImplementedError):
            c.messages.stream(model=SONNET, max_tokens=5, messages=MSG)

    def test_passthrough(self):
        c = self.make()
        self.assertEqual(c.models, "models-passthrough")
        self.assertEqual(c.messages.batches, "batches-passthrough")
        self.assertEqual(c.messages.count_tokens(model=SONNET, messages=MSG).input_tokens, 1000)

    def test_shared_budget_between_clients(self):
        a, b = self.make(), self.make(budget_usd=None)  # b keeps a's budget
        a.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        self.assertAlmostEqual(b.remaining(), a.remaining())

    # ---- Slack alerts without an Admin key ---------------------------------------
    def test_slack_alert_once_per_level(self):
        c = self.make(budget_usd=0.01)  # each call: actual $0.004, worst case $0.007
        c.messages.create(model=SONNET, max_tokens=0, messages=MSG)    # 40%: nothing
        self.assertEqual(self.posts, [])
        c.messages.create(model=SONNET, max_tokens=0, messages=MSG)    # 80%: one alert
        self.assertEqual(self.posts, [":warning: Claude budget: project *p* passed 80% of its budget "
                                      "($0.01 of $0.01 (80%))."])
        for _ in range(2):                                              # blocked twice, posted once
            with self.assertRaises(BudgetExceeded):
                c.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        self.assertEqual(len(self.posts), 2)
        self.assertIn("blocked a call", self.posts[1])

    def test_new_budget_rearms_alerts(self):
        c = self.make(budget_usd=0.008)
        c.messages.create(model=SONNET, max_tokens=0, messages=MSG)    # 50%
        c.messages.create(model=SONNET, max_tokens=0, messages=MSG)    # 100%
        self.assertEqual(len(self.posts), 2)
        self.ledger.set_budget("p", 0.016)                              # raised: back to 50%
        c.messages.create(model=SONNET, max_tokens=0, messages=MSG)    # 75%: no new level
        self.assertEqual(len(self.posts), 3)                            # 50% alert again for the new budget
        self.assertIn("passed 50%", self.posts[2])

    def test_run_budget_block_is_not_posted(self):
        c = self.make()
        with c.run_budget(0.001):
            with self.assertRaises(BudgetExceeded):
                c.messages.create(model=SONNET, max_tokens=500, messages=MSG)
        self.assertEqual(self.posts, [])

    def test_no_budget_no_alerts(self):
        BudgetedAnthropic("q", ledger=self.ledger, client=self.fake, multiplier=1.0, verbose=False,
                          notify=self.posts.append).messages.create(model=SONNET, max_tokens=0, messages=MSG)
        self.assertEqual(self.posts, [])

    # ---- usage parsing ------------------------------------------------------------
    def test_usage_parsing(self):
        u = NS(input_tokens=10, output_tokens=20, cache_read_input_tokens=30, cache_creation_input_tokens=99,
               cache_creation=NS(ephemeral_5m_input_tokens=40, ephemeral_1h_input_tokens=50),
               server_tool_use=NS(web_search_requests=2), inference_geo="us", speed=None)
        self.assertEqual(counts_from_usage(u, {}), Counts(10, 20, 30, 40, 50, 2, us_geo=True))
        legacy = NS(input_tokens=1, output_tokens=2, cache_read_input_tokens=None, cache_creation_input_tokens=7)
        self.assertEqual(counts_from_usage(legacy, {"speed": "fast"}), Counts(1, 2, 0, 7, fast=True))

    def test_wrapper_and_watchdog_agree(self):
        """Same tokens priced by the wrapper (per response) and the watchdog (per usage row)."""
        tokens = dict(uncached=5000, output=700, cache_read=20000, w5=3000, w1=1000, searches=2)
        usage = NS(input_tokens=5000, output_tokens=700, cache_read_input_tokens=20000,
                   cache_creation=NS(ephemeral_5m_input_tokens=3000, ephemeral_1h_input_tokens=1000),
                   server_tool_use=NS(web_search_requests=2))
        for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"):
            from claude_budget.pricing import cost_usd
            self.assertAlmostEqual(cost_usd(model, counts_from_usage(usage, {}), 0.5),
                                   row_cost(usage_row("k", model, **tokens), 0.5), msg=model)


if __name__ == "__main__":
    unittest.main()
