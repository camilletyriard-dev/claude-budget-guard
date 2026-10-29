# claude-budget-guard

Spend controls for the Claude API that the Console does not provide: **a budget per project in your code, and a budget per API key (or per person) across the whole lab.**

Console spend limits apply to a whole organization or workspace, not to a single API key. With one shared workspace, one runaway agent loop can use everybody's budget. This repo adds two layers:

| | **Wrapper** (`BudgetedAnthropic`) | **Watchdog** (`claude-budget watchdog`) |
|---|---|---|
| Who uses it | Every team member, in their own code | One admin, on a schedule |
| What it limits | A project (and optionally one agent run) | An API key, or all keys of one person |
| How | Refuses a call *before* it is sent if the worst case does not fit | Reads usage per key with the Admin API and sets the key to `inactive` |
| Speed | Immediate | Every ~15 min, plus a few minutes of usage-data lag |
| Covers | Only `messages.create` calls made through the wrapper | Everything on the key: SDK, notebooks, Batch, Claude Code |
| Slack | Alert at 50% / 80% of a project budget, and when a call is blocked | Alerts at 50% / 80%, when a key is disabled, and a **daily overview** |
| Needs | Nothing extra (set `SLACK_WEBHOOK_URL` for Slack) | An Admin API key (from an org admin) |

Use both: the wrapper is a fast, soft brake inside your code; the watchdog is the slower, hard stop on the key. **No Admin key yet?** The wrapper alone already gives you budgets and Slack alerts, see [Without an Admin key](#without-an-admin-key).

---

## For team members: add it to your code (2 minutes)

**1. Install** (needs read access to this repo)

```bash
pip install "git+https://github.com/camilletyriard-dev/claude-budget-guard.git"
```

**2. Set your key and your price** (Broad pays 50% of list price)

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # your own key, named person-project in Console
export CLAUDE_BUDGET_PRICE_MULTIPLIER=0.5
```

**3. Change one line**

```python
# before
import anthropic
client = anthropic.Anthropic()

# after
from claude_budget import BudgetedAnthropic
client = BudgetedAnthropic(project="rare-cancer", budget_usd=200)
```

`client.messages.create(...)` takes the same arguments and returns the same response. Every call now prints:

```
[claude-budget] estimate $0.0240 - $0.0640 (claude-opus-5-5; input 12,000 tok, output up to 4,000 tok)
[claude-budget] actual $0.0420 (in 12,000 / out 1,800 / cache read 0 / cache write 0) | spent $0.0420 | remaining $199.96
```

- **estimate low – high**: low = input only; high = worst case (input, at cache-write price if you use caching, plus the full `max_tokens` of output, plus web searches). Input tokens come from Anthropic's free token-count endpoint.
- If the worst case does not fit in the remaining budget, the call is **not sent** and you get `BudgetExceeded`.

### Options

```python
# ask before expensive calls (0 = always ask, None = never; default None)
client = BudgetedAnthropic(project="rare-cancer", budget_usd=200, confirm_above_usd=1.00)
# [claude-budget] worst case $1.6000, remaining $187.20. Continue? [y/N]

# cap one agent loop, on top of the project budget
with client.run_budget(5.00):
    run_my_agent(client)

# estimate without sending
est = client.estimate(model="claude-sonnet-5-5", max_tokens=2000, messages=msgs)
print(est.low_usd, est.high_usd)

client.status()      # {'project': ..., 'budget_usd': ..., 'spent_usd': ..., 'remaining_usd': ...}
client.verbose = False   # silence the printed lines
```

In a job without a keyboard (cluster, cron), an unanswered confirmation counts as **no**, so leave `confirm_above_usd` off there.

### Slack alerts from your code

```bash
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   # the lab channel's webhook
```

Then the wrapper posts once when a project passes 50% and 80% of its budget, and once when it blocks a call:

```
:warning: Claude budget: project *rare-cancer* passed 80% of its budget ($161.20 of $200.00 (81%)).
:no_entry: Claude budget: project *rare-cancer* blocked a call: budget used up ($199.10 of $200.00 (100%)).
```

Each alert is sent once per budget, even with many parallel processes; raising the budget re-arms them. Change the levels with `BudgetedAnthropic(..., alert_at=(0.5, 0.8, 0.95))`. A Slack error never stops your call.

### Command line

```bash
claude-budget status                    # budget / spent / remaining per project on this machine
claude-budget set rare-cancer 200       # set or change a budget ("none" removes it)
claude-budget history rare-cancer       # last calls
claude-budget add rare-cancer 12.50 --note "batch job"   # record spend made outside the wrapper
claude-budget report --slack            # post this machine's overview to Slack
```

(`python -m claude_budget ...` works too.)

### What the wrapper does not cover

- **Streaming** (`stream=True`, `messages.stream`): refused with a clear error rather than silently unbudgeted.
- **Batch API, async client, `beta` endpoints, Claude Code**: pass straight through, not budgeted. Record batch spend with `claude-budget add`; the watchdog sees all of these.
- **Budgets are per machine.** The ledger is a local SQLite file (`~/.claude_budget/ledger.db`, or `CLAUDE_BUDGET_DB`). Your laptop and the cluster each keep their own count. Don't put the ledger on a network drive (SQLite locking is unreliable there). Shared budgets across machines are the watchdog's job.
- **Server tools** (web search, code execution) can add input tokens inside one call that the estimate cannot see, so for those the "worst case" is not a strict upper bound.
- **Rounding**: costs are computed from the token counts in each response with the price table in `pricing.py`. Check against Console > Usage once (see below).

---

## For the admin: set up the watchdog

**1. Get an Admin API key** (`sk-ant-admin01-...`). Only an organization admin can create one, in Console. Workspace keys cannot read the usage report.

**2. Write the caps file**: copy `caps.example.toml` to `caps.toml`.

```toml
period = "total"          # since `start`, or "monthly"
start = "2026-10-01"
price_multiplier = 0.5
alert_at = [0.5, 0.8]     # Slack alerts
disable_at = 0.85         # key -> inactive (with --enforce)

[keys]                    # per API key name
"camille-rare-cancer" = 200

[people]                  # per person: all keys named "camille-..."
camille = 400
```

Key names must follow `person-project` for person caps to work.

**3. Try it (dry run: reports, never disables)**

```bash
export ANTHROPIC_ADMIN_KEY=sk-ant-admin01-...
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # optional
claude-budget watchdog --caps caps.toml
```

```
Spend since 2026-10-01 (DRY RUN):
  key kit-evolution                $    100.00 of $   100.00  (100.0%)
  -> would disable kit-evolution
  key camille-rare-cancer          $     14.00 of $   200.00  (  7.0%)
  person camille                   $     16.50 of $   400.00  (  4.1%)
```

**4. Run it every 15 minutes**, either

- **GitHub Actions** (no server needed): `.github/workflows/watchdog.yml`. Add the secrets `ANTHROPIC_ADMIN_KEY` and `SLACK_WEBHOOK_URL`, the variable `WATCHDOG_ENABLED=true`, commit `caps.toml`, and set `WATCHDOG_ENFORCE=true` once the dry run looks right. Scheduled runs can be delayed by GitHub.
- or **cron** on a lab machine: `*/15 * * * * cd /path && claude-budget watchdog --caps caps.toml --enforce`

**Daily overview in Slack**: every morning (12:41 UTC) the workflow posts one message with the spend per person and per key. Set `total_budget = 25000` in `caps.toml` to also see the share of the tranche used. Run it by hand from the Actions tab ("Run workflow").

```
:bar_chart: Claude API spend since 2026-10-01
Total $237.00 of $25,000.00 (1%) · yesterday $137.00
By person: *kit* $180.00 · *camille* $50.00 of $400.00 (12%)
By key: *kit-evolution* $180.00 of $400.00 (45%) · *camille-rare-cancer* $30.00 of $200.00 (15%) · *camille-scratch* $20.00
```

**What happens at the cap**: the key is set to `inactive` and Slack gets a message. To restore it, raise the cap in `caps.toml` *first*, then re-activate the key in Console. A key re-activated without a higher cap is switched off again on the next run.

**Limits of the watchdog**: usage data usually appears within ~5 minutes but can take longer, and it runs every 15 minutes, so a fast loop can overshoot a cap before the key is disabled. That is why `disable_at` is 85%, not 100%. Usage rows are daily aggregates, so the Haiku 5.5 long-prompt surcharge (5x above 100k prompt tokens per request) is not applied; models missing from the price table are priced at the most expensive model.

---

## Without an Admin key

The Admin key is the only way to see spend **per API key** across everyone. If the org admins won't create one, this still works without it:

1. **Wrapper + Slack** (above): budgets per project, alerts at 50% / 80% and on a block. Only covers code that uses the wrapper.
2. **Daily overview from a machine**: on the cluster (or wherever the jobs run), add a cron line:
   `41 8 * * * claude-budget report --slack`
   It posts that machine's projects, budgets and last-24-hour spend. Each machine reports its own ledger.
3. **Hard caps in Console**: a workspace per person or project with a Console spend limit. Enforced by Anthropic for every call, no code at all, but no Slack.

What you lose without the Admin key: calls that bypass the wrapper (raw SDK, notebooks, Claude Code) are not counted, and no key is ever disabled automatically.

**About the Admin key itself**: only someone with the *admin* role in the Console organization can create one (Console > Settings > Admin keys). The secret is shown once, at creation, so an existing key cannot be looked up later. Console Admin keys have no scopes: every key has full admin access to the organization. The safest setup is for the admin to paste the new key straight into this repo's GitHub secret (`ANTHROPIC_ADMIN_KEY`). Then nobody else ever sees it, and the watchdog code only reads usage, lists keys and sets a key to `inactive`.

## Keeping prices right

All prices live in one table, `src/claude_budget/pricing.py`, used by both the wrapper and the watchdog (a test checks they agree). Source: <https://platform.claude.com/docs/en/about-claude/pricing>, checked 2026-10-29. When a model is added or a price changes, edit that table and run the tests. An unknown model raises `UnknownModel` in the wrapper rather than guessing.

**First real check (do this once):** run `examples/quickstart.py` with a Development key, then compare the printed cost with Console > Usage for that key. They should match up to rounding.

## Tests

```bash
python -m unittest discover -s tests -t . -v     # no dependencies needed
```

54 tests, against a fake Anthropic client and a fake Admin API (no network, no cost): price table and modifiers (cache, Batch, US data residency, fast mode, Haiku 5.5 tier, web search), budget refusal before sending, confirmation prompt, run budgets, API errors releasing reservations, 8 processes racing for one budget without overshooting, watchdog pagination, alerts sent once per level (wrapper and watchdog), Slack failures never breaking a call, the daily overview, person caps, dry run vs enforce, and wrapper/watchdog price agreement.

**Not yet tested against the real API.** The usage-report and key-update requests follow the documented formats (links below); run the dry run once before enabling `--enforce`.

## References

- Pricing: <https://platform.claude.com/docs/en/about-claude/pricing>
- Usage and Cost API: <https://platform.claude.com/docs/en/manage-claude/usage-cost-api>
- Update API key: <https://platform.claude.com/docs/en/api/http/admin/api_keys/update>
- Token counting: <https://platform.claude.com/docs/en/build-with-claude/token-counting>
- Admin API keys: <https://platform.claude.com/docs/en/manage-claude/admin-api-keys>
- Spend limits API (workspace/organization only, early access): <https://platform.claude.com/docs/en/api/http/beta/organization/spend_limits/set>

See `PLAN.md` for the rollout plan.
