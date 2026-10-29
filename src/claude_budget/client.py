"""BudgetedAnthropic: a drop-in replacement for anthropic.Anthropic that

  * estimates each request's cost before sending it (free token-count endpoint),
  * optionally asks "Continue? [y/N]" above a cost threshold,
  * refuses the call if the worst case would exceed the project or run budget,
  * prints the actual cost and remaining budget afterwards,
  * records everything in a local SQLite ledger.

It cannot disable an API key; that is the watchdog's job (needs an Admin key).
"""
from __future__ import annotations

import contextvars
import json
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

from .ledger import BudgetExceeded, Ledger  # noqa: F401  (re-exported)
from .pricing import Counts, cost_usd, default_multiplier

PREFIX = "[claude-budget]"
COUNT_KEYS = ("model", "messages", "system", "tools", "tool_choice", "thinking")
DEFAULT_WEB_SEARCHES = 10  # worst case when a web_search tool has no max_uses
_run: contextvars.ContextVar[tuple[str, float] | None] = contextvars.ContextVar("claude_budget_run", default=None)


class CallCancelled(RuntimeError):
    """The user answered 'no' at the confirmation prompt."""


@dataclass
class Estimate:
    model: str
    input_tokens: int
    max_tokens: int
    low_usd: float   # input only, no caching
    high_usd: float  # input (at cache-write price if caching) + full max_tokens output + web searches
    exact_input: bool  # False if the token count fell back to a character heuristic


def _ask(prompt: str) -> bool:
    print(prompt, end=" ", file=sys.stderr, flush=True)
    try:
        return input().strip().lower() in ("y", "yes")
    except EOFError:  # no keyboard (batch job): treat as "no"
        return False


def _get(obj: Any, name: str, default: Any = 0) -> Any:
    """Read a field from an SDK object or a dict; None counts as missing."""
    value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return default if value is None else value


def _options(kwargs: dict) -> dict:
    """Request options, including ones passed through extra_body."""
    return {**(kwargs.get("extra_body") or {}), **kwargs}


def counts_from_usage(usage: Any, kwargs: dict) -> Counts:
    """Translate a response's `usage` into billable Counts."""
    opts = _options(kwargs)
    cc = _get(usage, "cache_creation", None)
    w5, w1 = (_get(cc, "ephemeral_5m_input_tokens"), _get(cc, "ephemeral_1h_input_tokens")) if cc is not None \
        else (_get(usage, "cache_creation_input_tokens"), 0)
    return Counts(
        input=_get(usage, "input_tokens"), output=_get(usage, "output_tokens"),
        cache_read=_get(usage, "cache_read_input_tokens"), cache_write_5m=w5, cache_write_1h=w1,
        web_searches=_get(_get(usage, "server_tool_use", None) or {}, "web_search_requests"),
        us_geo=(_get(usage, "inference_geo", None) or opts.get("inference_geo")) == "us",
        fast=(_get(usage, "speed", None) or opts.get("speed")) == "fast",
    )


class _Messages:
    def __init__(self, owner: "BudgetedAnthropic"):
        self._o = owner

    def create(self, **kwargs: Any) -> Any:
        return self._o._create(**kwargs)

    def stream(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError("Streaming is not budgeted yet. Use messages.create(...) without stream.")

    def __getattr__(self, name: str) -> Any:  # count_tokens, batches, ... pass through unbudgeted
        return getattr(self._o._client.messages, name)


class BudgetedAnthropic:
    """Use exactly like anthropic.Anthropic; only messages.create is budgeted.

    project:            ledger key, e.g. "rare-cancer"
    budget_usd:         cap for the project on this machine (None: keep what the ledger has)
    confirm_above_usd:  ask before calls whose worst case is >= this (0: always, None: never)
    multiplier:         your price / list price (default env CLAUDE_BUDGET_PRICE_MULTIPLIER, else 1.0)
    """

    def __init__(self, project: str, budget_usd: float | None = None, *, confirm_above_usd: float | None = None,
                 multiplier: float | None = None, ledger: Ledger | None = None, client: Any = None,
                 confirm: Callable[[str], bool] = _ask, verbose: bool = True, **client_kwargs: Any):
        if client is None:
            import anthropic  # imported lazily so tests and the watchdog don't need the SDK
            client = anthropic.Anthropic(**client_kwargs)
        self._client, self.project, self.verbose = client, project, verbose
        self.confirm_above_usd, self._confirm = confirm_above_usd, confirm
        self.multiplier = default_multiplier() if multiplier is None else multiplier
        self.ledger = ledger or Ledger()
        if budget_usd is not None:
            self.ledger.set_budget(project, budget_usd)
        self.messages = _Messages(self)

    def __getattr__(self, name: str) -> Any:  # models, beta, ... pass through unbudgeted
        return getattr(self._client, name)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"{PREFIX} {msg}", file=sys.stderr)

    # ---- status -------------------------------------------------------------
    def remaining(self) -> float | None:
        budget = self.ledger.budget(self.project)
        return None if budget is None else budget - self.ledger.spent(self.project)

    def status(self) -> dict:
        budget, spent = self.ledger.budget(self.project), self.ledger.spent(self.project)
        return {"project": self.project, "budget_usd": budget, "spent_usd": spent,
                "remaining_usd": None if budget is None else budget - spent}

    # ---- estimate -------------------------------------------------------------
    def estimate(self, **kwargs: Any) -> Estimate:
        model, max_tokens = kwargs["model"], int(kwargs.get("max_tokens", 0))
        try:
            n = _get(self._client.messages.count_tokens(**{k: kwargs[k] for k in COUNT_KEYS if k in kwargs}),
                     "input_tokens")
            exact = True
        except Exception as e:  # rate limit, network, unsupported params: stay conservative
            n = len(json.dumps({k: kwargs.get(k) for k in COUNT_KEYS}, default=str)) // 3 + 1
            exact = False
            self._log(f"token count failed ({type(e).__name__}); using a rough estimate of {n:,} input tokens")
        blob = json.dumps(kwargs, default=str)
        searches = sum(int(_get(t, "max_uses", DEFAULT_WEB_SEARCHES)) for t in kwargs.get("tools") or []
                       if str(_get(t, "type", "")).startswith("web_search"))
        opts = _options(kwargs)
        flags = dict(us_geo=opts.get("inference_geo") == "us", fast=opts.get("speed") == "fast")
        if '"cache_control"' not in blob:
            worst_input = dict(input=n)
        elif '"1h"' in blob:
            worst_input = dict(cache_write_1h=n)
        else:
            worst_input = dict(cache_write_5m=n)
        low = cost_usd(model, Counts(input=n, **flags), self.multiplier)
        high = cost_usd(model, Counts(output=max_tokens, web_searches=searches, **worst_input, **flags),
                        self.multiplier)
        return Estimate(model, n, max_tokens, low, high, exact)

    # ---- the budgeted call -----------------------------------------------------
    def _create(self, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            raise NotImplementedError("stream=True is not budgeted yet. Call without stream.")
        est = self.estimate(**kwargs)
        self._log(f"estimate ${est.low_usd:.4f} - ${est.high_usd:.4f} ({est.model}; input "
                  f"{'' if est.exact_input else '~'}{est.input_tokens:,} tok, output up to {est.max_tokens:,} tok)")
        if self.confirm_above_usd is not None and est.high_usd >= self.confirm_above_usd:
            left = self.remaining()
            msg = f"{PREFIX} worst case ${est.high_usd:.4f}" + ("" if left is None else f", remaining ${left:.2f}")
            if not self._confirm(msg + ". Continue? [y/N]"):
                raise CallCancelled("Call cancelled at the confirmation prompt.")
        run_id, run_limit = _run.get() or (None, None)
        entry = self.ledger.reserve(self.project, est.high_usd, est.model, run_id, run_limit)
        try:
            response = self._client.messages.create(**kwargs)
        except BaseException as e:
            self.ledger.release(entry, f"error: {type(e).__name__}")
            raise
        c = counts_from_usage(response.usage, kwargs)
        cost = cost_usd(est.model, c, self.multiplier)
        self.ledger.settle(entry, cost, _get(response, "id", None))
        left = self.remaining()
        self._log(f"actual ${cost:.4f} (in {c.input:,} / out {c.output:,} / cache read {c.cache_read:,} / "
                  f"cache write {c.cache_write_5m + c.cache_write_1h:,}) | spent ${self.ledger.spent(self.project):.4f}"
                  + ("" if left is None else f" | remaining ${left:.2f}"))
        return response

    @contextmanager
    def run_budget(self, usd: float, name: str | None = None):
        """Cap one agent run: `with client.run_budget(5.00): ...` (calls refused beyond $5)."""
        run_id = name or f"run-{uuid.uuid4().hex[:8]}"
        token = _run.set((run_id, usd))
        try:
            yield run_id
        finally:
            _run.reset(token)
            self._log(f"run {run_id}: spent ${self.ledger.run_spent(run_id):.4f} of ${usd:.2f}")
