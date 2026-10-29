"""Command line: `claude-budget status | set | history | add | watchdog`."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from .ledger import Ledger


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-budget", description="Local Claude API budgets and the key watchdog.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="budget, spent and remaining per project on this machine")
    s = sub.add_parser("set", help="set a project budget (use 'none' to remove the cap)")
    s.add_argument("project"); s.add_argument("usd")
    r = sub.add_parser("report", help="overview of this machine's projects (no Admin key needed)")
    r.add_argument("--slack", action="store_true", help="also post it to SLACK_WEBHOOK_URL")
    h = sub.add_parser("history", help="last calls for a project")
    h.add_argument("project"); h.add_argument("-n", type=int, default=20)
    a = sub.add_parser("add", help="record spend made outside the wrapper (negative to correct)")
    a.add_argument("project"); a.add_argument("usd", type=float); a.add_argument("--note")
    w = sub.add_parser("watchdog", help="check per-key spend with the Admin API (needs ANTHROPIC_ADMIN_KEY)")
    w.add_argument("--caps", default="caps.toml"); w.add_argument("--enforce", action="store_true",
                                                                   help="actually disable keys (default: dry run)")
    w.add_argument("--summary", action="store_true", help="also post the daily spend overview to Slack")
    args = p.parse_args(argv)

    if args.cmd == "watchdog":
        from .watchdog import AdminAPI, Config, run
        key = os.environ.get("ANTHROPIC_ADMIN_KEY")
        if not key:
            print("Set ANTHROPIC_ADMIN_KEY (an Admin API key, sk-ant-admin01-...).", file=sys.stderr)
            return 2
        run(Config.load(args.caps), AdminAPI(key), enforce=args.enforce, summary=args.summary)
        return 0

    ledger = Ledger()
    if args.cmd == "status":
        print(f"{'project':<24}{'budget':>12}{'spent':>12}{'remaining':>12}   ({ledger.path})")
        for project, budget, spent in ledger.summary():
            left = "-" if budget is None else f"${budget - spent:,.2f}"
            print(f"{project:<24}{'-' if budget is None else f'${budget:,.2f}':>12}{f'${spent:,.4f}':>12}{left:>12}")
    elif args.cmd == "report":
        from .notify import overview, slack
        day_ago = time.time() - 86400
        rows = [(p, b, s, ledger.spent_since(p, day_ago)) for p, b, s in ledger.summary()]
        text = overview(f":bar_chart: *Claude API spend on {os.uname().nodename}* · last 24 h "
                        f"${sum(r[3] for r in rows):,.2f}", {"By project": [(p, s, b) for p, b, s, _ in rows if s]})
        print(text)
        if args.slack and not slack(text):
            print("Not posted: set SLACK_WEBHOOK_URL.", file=sys.stderr)
            return 1
    elif args.cmd == "set":
        ledger.set_budget(args.project, None if args.usd.lower() == "none" else float(args.usd))
    elif args.cmd == "add":
        ledger.add_manual(args.project, args.usd, args.note)
    elif args.cmd == "history":
        for ts, run_id, model, status, reserved, cost, note in ledger.history(args.project, args.n):
            print(f"{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}  {status:<9}{model or '':<20}"
                  f"${cost:>9.4f}  {run_id or ''} {note or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
