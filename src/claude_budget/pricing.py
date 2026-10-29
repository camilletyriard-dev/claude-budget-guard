"""Price table and cost function, shared by the wrapper and the watchdog.

List prices in USD per million tokens, from
https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-29).
Update this file when prices change; everything else reads from it.

Rules applied (from the same page):
  * cache writes: 1.25x base input (5-minute TTL), 2x (1-hour TTL)
  * cache reads: listed per model (usually 0.1x base input)
  * Batch API: 50% off input and output
  * inference_geo="us": 1.1x on everything (Claude 4.6 and later)
  * fast mode: 2x input and output (Opus 5.5: $8/$40; Opus 5 / 4.8: $10/$50)
  * Haiku 5.5: requests with >100k prompt tokens pay 5x on everything
  * web search: $0.01 per search
Your own discount (e.g. Broad's 50%) is applied on top via `multiplier`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# model id prefix -> (input, output, cache_read) in USD per million tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10, 50, 0.25),
    "claude-mythos-5-1": (10, 50, 0.25),
    "claude-opus-5-5": (4, 20, 0.20),
    "claude-sonnet-5-5": (2, 10, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5": (10, 50, 1.00),
    "claude-mythos-5": (10, 50, 1.00),
    "claude-opus-5": (5, 25, 0.50),
    "claude-opus-4-8": (5, 25, 0.50),
    "claude-opus-4-7": (5, 25, 0.50),
    "claude-opus-4-6": (5, 25, 0.50),
    "claude-opus-4-5": (5, 25, 0.50),
    "claude-opus-4-1": (15, 75, 1.50),
    "claude-opus-4": (15, 75, 1.50),
    "claude-opus-4-0": (15, 75, 1.50),
    "claude-sonnet-5": (2, 10, 0.20),  # cache read listed as $0.20, not 0.1x
    "claude-sonnet-4-6": (3, 15, 0.30),
    "claude-sonnet-4-5": (3, 15, 0.30),
    "claude-sonnet-4": (3, 15, 0.30),
    "claude-sonnet-4-0": (3, 15, 0.30),
    "claude-haiku-4-5": (1, 5, 0.10),
    "claude-3-5-haiku": (0.80, 4, 0.08),
}
CACHE_WRITE_5M, CACHE_WRITE_1H = 1.25, 2.0
BATCH, US_GEO, FAST = 0.5, 1.1, 2.0
WEB_SEARCH_USD = 0.01
HAIKU_55_TIER = ("claude-haiku-5-5", 100_000, 5.0)  # model, prompt-token threshold, factor


class UnknownModel(KeyError):
    """No price for this model. Add it to PRICES rather than guessing."""


@dataclass
class Counts:
    """Token counts for one request (or the sum of several with the same settings)."""
    input: int = 0            # uncached input tokens
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    web_searches: int = 0
    batch: bool = False
    us_geo: bool = False
    fast: bool = False

    @property
    def prompt_tokens(self) -> int:
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h


def default_multiplier() -> float:
    """Your price as a fraction of list price. Broad: CLAUDE_BUDGET_PRICE_MULTIPLIER=0.5."""
    return float(os.environ.get("CLAUDE_BUDGET_PRICE_MULTIPLIER", "1.0"))


def lookup(model: str) -> tuple[float, float, float]:
    """Exact id, or id plus a date/`latest` suffix (claude-sonnet-4-5-20250929)."""
    base = re.sub(r"\[.*\]$", "", model)  # Claude Code style "claude-opus-5-5[1m]"
    base = re.sub(r"-(\d{8}|latest)$", "", base)
    if base not in PRICES:
        raise UnknownModel(f"No price for model {model!r}. Add it to claude_budget.pricing.PRICES.")
    return PRICES[base]


def cost_usd(model: str, c: Counts, multiplier: float | None = None, aggregate: bool = False) -> float:
    """Cost of `c` in USD at list price times `multiplier` (default: env setting).

    aggregate=True: `c` sums many requests, so the per-request Haiku 5.5 tier cannot
    be applied (the watchdog uses this; see its docstring).
    """
    inp, out, read = lookup(model)
    tokens = (c.input * inp + c.output * out + c.cache_read * read
              + c.cache_write_5m * inp * CACHE_WRITE_5M + c.cache_write_1h * inp * CACHE_WRITE_1H) / 1e6
    factor = (BATCH if c.batch else 1) * (US_GEO if c.us_geo else 1) * (FAST if c.fast else 1)
    name, threshold, tier = HAIKU_55_TIER
    if not aggregate and model.startswith(name) and c.prompt_tokens > threshold:
        factor *= tier
    total = tokens * factor + c.web_searches * WEB_SEARCH_USD
    return total * (default_multiplier() if multiplier is None else multiplier)
