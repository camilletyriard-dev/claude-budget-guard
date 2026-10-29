"""Slack messages (incoming webhook) and the text of alerts and overviews."""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def slack(text: str, url: str | None = None) -> bool:
    """Post `text` to SLACK_WEBHOOK_URL. Never raises: budgets must not fail because Slack did."""
    url = url or os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                     headers={"content-type": "application/json"})
        urllib.request.urlopen(req, timeout=30).close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[claude-budget] Slack post failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def level(frac: float, levels: list[float]) -> float:
    """Highest alert level reached (0 if none)."""
    return max([lv for lv in levels if frac >= lv], default=0.0)


def money(spent: float, cap: float | None = None) -> str:
    return f"${spent:,.2f}" if not cap else f"${spent:,.2f} of ${cap:,.2f} ({spent / cap:.0%})"


def overview(title: str, sections: dict[str, list[tuple[str, float, float | None]]], top: int = 10) -> str:
    """Slack text: a title line, then one line per section with `label money` items, biggest first."""
    lines = [title]
    for name, items in sections.items():
        items = sorted(items, key=lambda i: -i[1])
        if not items:
            continue
        shown = " · ".join(f"*{label}* {money(spent, cap)}" for label, spent, cap in items[:top])
        more = f" · +{len(items) - top} more" if len(items) > top else ""
        lines.append(f"{name}: {shown}{more}")
    return "\n".join(lines)
