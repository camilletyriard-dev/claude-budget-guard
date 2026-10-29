# Rollout plan

Goal: no single person, project or runaway loop can use more than its share of the award credits, without creating a workspace per project.

## What the platform gives us (checked 2026-10-29)

- **Console spend limits**: per organization and per workspace (hard, enforced by Anthropic). Our Dev ($1K) / Prod ($24K) workspaces already use these. Keep them as the outer backstop.
- **Spend limits API**: can set organization and workspace limits, in early access for Console organizations. Per-member (`user`) limits exist only for Claude Enterprise organizations. **No per-API-key limit.**
- **Admin API**: can read usage per API key and set a key to `inactive`. This is what the watchdog uses.

So per-key and per-person caps have to be built by us: that is this repo.

## Phases

| Phase | When | What | Who | Done when |
|---|---|---|---|---|
| 0. Prerequisites | Now | Admin API key from the Broad admins; Slack incoming webhook for the lab channel; decide where the repo lives (lab org recommended) | Camille asks, Broad admins / Ignacio approve | Key and webhook stored as repo secrets |
| 1. Wrapper pilot | Week 1 | Each person installs the package, runs `examples/quickstart.py` on a Dev key, compares printed cost with Console > Usage | Everyone | Costs match Console up to rounding for at least one call per person |
| 2. Watchdog dry run | Week 1-2 | Run on GitHub Actions without `--enforce`; compare per-key totals with Console > Usage once a day | Camille | One week of totals within ~2% of Console; no false alerts |
| 3. Enforce | Week 2 | Set `WATCHDOG_ENFORCE=true`, Production workspace first | Camille, with Ignacio's OK on the caps | One deliberate test: a $1-cap test key gets disabled |
| 4. Ground rules | Week 2 | Add to lab rules: wrapper in all API code; key names `person-project`; runs > $100 posted in the channel first (kickoff rule) | Ignacio | In the onboarding doc |

## Decisions to make

1. **Caps per key / per person** for the first $25K tranche (suggestion: per-person caps in Production, a small default cap for unlisted keys, Dev covered by its $1K workspace limit).
2. **`disable_at`**: 85% leaves room for the 15-25 minute detection lag. Lower it for people running large parallel jobs.
3. **Who re-enables keys** after a cap is hit (suggestion: Ignacio approves, Camille edits `caps.toml`).

## How to manage it: Console, Slack, or an add-on?

| Option | What it adds | Effort | Recommendation |
|---|---|---|---|
| **Console** | Creating/naming keys, workspace caps, re-enabling a disabled key, Usage pages to check numbers | None | **Use for structure and manual actions.** It cannot hold per-key caps. |
| **Slack alerts** (webhook) | Messages at 50% / 80% / disable | Done (`SLACK_WEBHOOK_URL`) | **Use.** Cheapest way for everyone to see spend. |
| **Slack commands** (e.g. `/budget status`, `/budget raise kit 50`) | Manage caps from Slack | 2-4 days + a hosted Slack app, and someone has to own its security | **Not now.** Editing `caps.toml` in a pull request gives the same control with review and history. |
| **GitHub as the control panel** | Caps changed by pull request; Ignacio approves; Actions runs the watchdog | Done | **Use.** Auditable, no server. |
| **Proxy / gateway** (e.g. LiteLLM with virtual keys) | Hard per-key limits *before* each call, for any tool incl. Claude Code | 1-3 days + hosting + everyone pointing `ANTHROPIC_BASE_URL` at it | **Only if** the watchdog's lag proves too loose (e.g. large Claude Code usage on API keys). |
| **Dashboard** | One page with spend per person / key over time | ~1 day | Optional later; the watchdog output already has the numbers. |

**Recommended setup:** Console (workspaces + hard workspace caps) + watchdog on GitHub Actions with Slack alerts + wrapper in everyone's code. Revisit a proxy only after a month of data.

## Risks

- **Admin key is powerful** (it can manage the whole organization). Store it only as a GitHub secret in a private repo with few admins; rotate it if anyone leaves.
- **Detection lag**: the watchdog is not a hard real-time limit. The wrapper's run budgets and the workspace caps cover the fast cases.
- **Price drift**: if prices change and `pricing.py` is not updated, both tools are off by the same amount. Phase 2's comparison with Console catches this.
- **Untested against the live API**: the request formats follow the docs, but phases 1-2 are the real test.
