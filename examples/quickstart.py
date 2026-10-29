"""Smallest real test: one cheap call, then compare the printed cost with Console > Usage.

    export ANTHROPIC_API_KEY=sk-ant-...            # your Development workspace key
    export CLAUDE_BUDGET_PRICE_MULTIPLIER=0.5      # Broad pays 50% of list price
    python examples/quickstart.py
"""
from claude_budget import BudgetedAnthropic

client = BudgetedAnthropic(project="quickstart", budget_usd=1.00, confirm_above_usd=0.10)

response = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=100,
    messages=[{"role": "user", "content": "In one sentence: what is Li-Fraumeni syndrome?"}],
)
print(response.content[0].text)
print(client.status())
