"""Cap one agent loop at $2, independent of the project budget."""
from claude_budget import BudgetedAnthropic, BudgetExceeded

client = BudgetedAnthropic(project="rare-cancer", budget_usd=200)
messages = [{"role": "user", "content": "List three tumour-suppressor genes, one per turn. Say DONE after the third."}]

try:
    with client.run_budget(2.00, name="demo-loop"):
        for _ in range(10):
            reply = client.messages.create(model="claude-sonnet-5-5", max_tokens=200, messages=messages)
            text = reply.content[0].text
            print(text)
            if "DONE" in text:
                break
            messages += [{"role": "assistant", "content": text}, {"role": "user", "content": "Next."}]
except BudgetExceeded as e:
    print("Stopped:", e)
