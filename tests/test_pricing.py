import os
import unittest
from unittest import mock

from claude_budget.pricing import PRICES, Counts, UnknownModel, cost_usd, lookup

M = 1_000_000


class PricingTest(unittest.TestCase):
    def test_base_input_output(self):
        self.assertAlmostEqual(cost_usd("claude-opus-5-5", Counts(input=M, output=M), 1.0), 4 + 20)

    def test_broad_discount_matches_kickoff_slide(self):
        # Kickoff slide 8: "our rate" = list minus 50%
        for model, (inp, out) in {"claude-haiku-4-5": (0.5, 2.5), "claude-sonnet-5-5": (1, 5),
                                  "claude-opus-5-5": (2, 10), "claude-fable-5-1": (5, 25)}.items():
            self.assertAlmostEqual(cost_usd(model, Counts(input=M), 0.5), inp, msg=model)
            self.assertAlmostEqual(cost_usd(model, Counts(output=M), 0.5), out, msg=model)

    def test_cache_prices(self):
        c = cost_usd("claude-sonnet-5-5", Counts(cache_read=M, cache_write_5m=M, cache_write_1h=M), 1.0)
        self.assertAlmostEqual(c, 0.10 + 2 * 1.25 + 2 * 2.0)

    def test_modifiers_stack(self):
        base = cost_usd("claude-opus-5-5", Counts(input=M, output=M), 1.0)
        self.assertAlmostEqual(cost_usd("claude-opus-5-5", Counts(input=M, output=M, batch=True), 1.0), base / 2)
        self.assertAlmostEqual(cost_usd("claude-opus-5-5", Counts(input=M, output=M, us_geo=True), 1.0), base * 1.1)
        self.assertAlmostEqual(cost_usd("claude-opus-5-5", Counts(input=M, output=M, fast=True), 1.0), 8 + 40)

    def test_haiku_55_long_prompt_tier(self):
        small = cost_usd("claude-haiku-5-5", Counts(input=100_000), 1.0)
        big = cost_usd("claude-haiku-5-5", Counts(input=100_001), 1.0)
        self.assertAlmostEqual(small, 0.01)
        self.assertAlmostEqual(big, 100_001 * 0.50 / M)  # 5x: $0.50 per million
        self.assertAlmostEqual(cost_usd("claude-haiku-5-5", Counts(input=100_001), 1.0, aggregate=True),
                               100_001 * 0.10 / M)
        # cache reads count toward the threshold
        self.assertGreater(cost_usd("claude-haiku-5-5", Counts(input=10, cache_read=100_000), 1.0),
                           cost_usd("claude-haiku-5-5", Counts(input=10, cache_read=99_990), 1.0) * 4)

    def test_web_search(self):
        self.assertAlmostEqual(cost_usd("claude-sonnet-5-5", Counts(web_searches=10), 0.5), 0.05)

    def test_model_lookup(self):
        self.assertEqual(lookup("claude-opus-5-5"), PRICES["claude-opus-5-5"])
        self.assertEqual(lookup("claude-opus-5"), PRICES["claude-opus-5"])  # not confused with 5-5
        self.assertEqual(lookup("claude-sonnet-4-5-20250929"), PRICES["claude-sonnet-4-5"])
        self.assertEqual(lookup("claude-opus-4-20250514"), PRICES["claude-opus-4"])
        self.assertEqual(lookup("claude-opus-5-5[1m]"), PRICES["claude-opus-5-5"])
        self.assertEqual(lookup("claude-3-5-haiku-latest"), PRICES["claude-3-5-haiku"])
        with self.assertRaises(UnknownModel):
            lookup("claude-opus-5-55")
        with self.assertRaises(UnknownModel):
            lookup("gpt-5")

    def test_env_multiplier(self):
        with mock.patch.dict(os.environ, {"CLAUDE_BUDGET_PRICE_MULTIPLIER": "0.5"}):
            self.assertAlmostEqual(cost_usd("claude-opus-5-5", Counts(input=M)), 2.0)

    def test_table_is_sane(self):
        for model, (inp, out, read) in PRICES.items():
            self.assertAlmostEqual(out, 5 * inp, msg=model)  # every listed model: output = 5x input
            self.assertLessEqual(read, inp, msg=model)


if __name__ == "__main__":
    unittest.main()
