"""Watchdog: per-API-key (and per-person) budgets enforced with the Admin API.

Every run it
  1. lists the workspace's API keys (id -> name),
  2. pulls token usage per key since the period start (Usage report, 1-day buckets),
  3. prices it with the shared price table,
  4. posts Slack alerts at each `alert_at` fraction of a cap (once per period),
  5. sets a key to "inactive" at `disable_at` -- only with --enforce (dry run otherwise).

It sees ALL usage on a key: the wrapper, raw SDK calls, notebooks, Batch and Claude Code.
Limits: usage data lags (~5 min, sometimes more) and it runs on a schedule, so a fast
loop can overshoot before the key is disabled. Usage rows are aggregated, so the
per-request Haiku 5.5 long-prompt tier (5x above 100k prompt tokens) is not applied;
unknown models are priced at the most expensive model (overestimate, on purpose).

Admin API docs: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
"""
from __future__ import annotations

import json
import sys
import tomllib
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .notify import level, money, overview, slack
from .pricing import PRICES, Counts, UnknownModel, cost_usd

API = "https://api.anthropic.com/v1/organizations"
MOST_EXPENSIVE = max(PRICES, key=lambda m: PRICES[m][1])


@dataclass
class Config:
    keys: dict[str, float] = field(default_factory=dict)    # key name -> cap USD
    people: dict[str, float] = field(default_factory=dict)  # person -> cap USD (keys named "<person>-...")
    default_key_cap: float | None = None
    period: str = "total"            # "total" (since `start`) or "monthly" (calendar month, UTC)
    start: str = "2026-01-01"
    price_multiplier: float = 1.0
    alert_at: list[float] = field(default_factory=lambda: [0.5, 0.8])
    disable_at: float = 0.85
    workspace_id: str | None = None
    total_budget: float | None = None  # shown in the daily overview, e.g. 25000
    state_file: str = "watchdog_state.json"

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path, "rb") as f:
            return cls(**tomllib.load(f))

    def period_start(self, now: datetime) -> datetime:
        if self.period == "monthly":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if self.period == "total":
            return datetime.fromisoformat(self.start).replace(tzinfo=timezone.utc)
        raise ValueError(f"period must be 'total' or 'monthly', not {self.period!r}")


class AdminAPI:
    """Minimal Admin API client (stdlib only). Tests replace `request`."""

    def __init__(self, admin_key: str):
        self.headers = {"x-api-key": admin_key, "anthropic-version": "2023-06-01",
                        "content-type": "application/json", "user-agent": "claude-budget-guard/0.1"}

    def request(self, method: str, path: str, params: list[tuple[str, str]] | None = None,
                body: dict | None = None) -> dict:
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(url, method=method, headers=self.headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def list_keys(self, workspace_id: str | None) -> list[dict]:
        keys, after = [], None
        while True:
            params = [("limit", "1000")] + ([("workspace_id", workspace_id)] if workspace_id else []) \
                     + ([("after_id", after)] if after else [])
            page = self.request("GET", "/api_keys", params)
            keys += page["data"]
            if not page.get("has_more"):
                return keys
            after = page["last_id"]

    def usage_rows(self, start: datetime, workspace_id: str | None) -> list[dict]:
        rows, page_token = [], None
        base = [("starting_at", start.strftime("%Y-%m-%dT%H:%M:%SZ")), ("bucket_width", "1d"), ("limit", "31")]
        base += [("group_by[]", g) for g in ("api_key_id", "model", "service_tier", "inference_geo", "speed")]
        base += [("workspace_ids[]", workspace_id)] if workspace_id else []
        while True:
            page = self.request("GET", "/usage_report/messages", base + ([("page", page_token)] if page_token else []))
            rows += [dict(r, day=b["starting_at"][:10]) for b in page["data"] for r in b["results"]]
            if not page.get("has_more"):
                return rows
            page_token = page["next_page"]

    def set_key_status(self, key_id: str, status: str) -> dict:
        return self.request("POST", f"/api_keys/{key_id}", body={"status": status})


def row_cost(row: dict, multiplier: float) -> float:
    cc = row.get("cache_creation") or {}
    counts = Counts(input=row.get("uncached_input_tokens") or 0, output=row.get("output_tokens") or 0,
                    cache_read=row.get("cache_read_input_tokens") or 0,
                    cache_write_5m=cc.get("ephemeral_5m_input_tokens") or 0,
                    cache_write_1h=cc.get("ephemeral_1h_input_tokens") or 0,
                    web_searches=(row.get("server_tool_use") or {}).get("web_search_requests") or 0,
                    batch=row.get("service_tier") == "batch", us_geo=row.get("inference_geo") == "us",
                    fast=row.get("speed") == "fast")
    try:
        return cost_usd(row.get("model") or "", counts, multiplier, aggregate=True)
    except UnknownModel:
        return cost_usd(MOST_EXPENSIVE, counts, multiplier, aggregate=True)


@dataclass
class Target:
    """Something with a cap: one key, or one person (several keys)."""
    label: str
    cap: float
    key_ids: list[str]
    spent: float = 0.0

    @property
    def frac(self) -> float:
        return self.spent / self.cap if self.cap > 0 else float("inf")


def person_of(key_name: str) -> str:
    return key_name.split("-", 1)[0].lower()


def build_targets(cfg: Config, keys: list[dict], spend_by_id: dict[str, float]) -> list[Target]:
    targets: list[Target] = []
    for k in keys:
        cap = cfg.keys.get(k["name"], cfg.default_key_cap)
        if cap is not None:
            targets.append(Target(f"key {k['name']}", cap, [k["id"]], spend_by_id.get(k["id"], 0.0)))
    for person, cap in cfg.people.items():
        ids = [k["id"] for k in keys if person_of(k["name"]) == person.lower()]
        targets.append(Target(f"person {person}", cap, ids, sum(spend_by_id.get(i, 0.0) for i in ids)))
    return targets


def daily_overview(cfg: Config, names: dict[str, str], spend: dict[str, float], yday: dict[str, float],
                   since: str) -> str:
    """One Slack message: total, yesterday, spend per person and per key (with caps where set)."""
    label = lambda i: names.get(i, "playground / other workspace")  # noqa: E731
    people: dict[str, float] = defaultdict(float)
    for key_id, usd in spend.items():
        people[person_of(label(key_id))] += usd
    caps = {p.lower(): c for p, c in cfg.people.items()}
    title = (f":bar_chart: *Claude API spend since {since}*\nTotal {money(sum(spend.values()), cfg.total_budget)}"
             f" · yesterday ${sum(yday.values()):,.2f}")
    return overview(title, {
        "By person": [(p, usd, caps.get(p)) for p, usd in people.items() if usd > 0],
        "By key": [(label(i), usd, cfg.keys.get(label(i), cfg.default_key_cap)) for i, usd in spend.items() if usd > 0],
    })


def run(cfg: Config, api: AdminAPI, enforce: bool = False, now: datetime | None = None,
        notify=slack, out=sys.stdout, summary: bool = False) -> list[Target]:
    now = now or datetime.now(timezone.utc)
    start = cfg.period_start(now)
    keys = api.list_keys(cfg.workspace_id)
    names = {k["id"]: k["name"] for k in keys}
    status = {k["id"]: k["status"] for k in keys}

    spend: dict[str, float] = defaultdict(float)
    yday: dict[str, float] = defaultdict(float)
    yesterday = (now - timedelta(days=1)).date().isoformat()
    for row in api.usage_rows(start, cfg.workspace_id):
        key_id, usd = row.get("api_key_id") or "playground", row_cost(row, cfg.price_multiplier)
        spend[key_id] += usd
        if row.get("day") == yesterday:
            yday[key_id] += usd
    targets = build_targets(cfg, keys, spend)

    state_path = Path(cfg.state_file)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    period_tag = start.date().isoformat()

    print(f"Spend since {period_tag} ({'enforce' if enforce else 'DRY RUN'}):", file=out)
    for t in sorted(targets, key=lambda t: -t.frac):
        print(f"  {t.label:<32} ${t.spent:>10.2f} of ${t.cap:>9.2f}  ({t.frac:6.1%})", file=out)
        over = t.frac >= cfg.disable_at
        active = [i for i in t.key_ids if status.get(i) == "active"] if over else []
        for key_id in active:  # every run, so a key re-enabled without raising its cap goes off again
            if enforce:
                api.set_key_status(key_id, "inactive")
                status[key_id] = "inactive"
            print(f"  -> {'disabled' if enforce else 'would disable'} {names[key_id]}", file=out)
        # one message per level per period; a disable is always reported
        tag = f"{period_tag}|{t.label}"
        reached = cfg.disable_at if over else level(t.frac, cfg.alert_at)
        if reached > state.get(tag, 0) or (active and enforce):
            action = (f" Disabled: {', '.join(names[i] for i in active)}." if active and enforce else
                      f" Would disable: {', '.join(names[i] for i in active)} (dry run)." if active else "")
            notify(f"{':no_entry:' if over else ':warning:'} Claude budget: {t.label} at {t.frac:.0%} "
                   f"(${t.spent:.2f} of ${t.cap:.2f}).{action}")
            state[tag] = max(reached, state.get(tag, 0))
    unpriced = sum(v for k, v in spend.items() if k not in names)
    if unpriced:
        print(f"  (${unpriced:.2f} on keys outside this workspace or the Console playground)", file=out)
    state_path.write_text(json.dumps(state, indent=1))
    if summary:
        notify(daily_overview(cfg, names, spend, yday, period_tag))
    return targets
