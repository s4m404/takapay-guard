# takapay-guard

A small, rule-based check for tool-using AI agents. Before an agent runs a
sensitive tool call (send money, send an email, delete a file), the guard
checks where each important argument came from. If a payee, amount,
address or code appears only in untrusted text (a tool result, an SMS, a
web page, a retrieved document) and never in the user's own request, the
call is blocked or the user is asked, with a reason that names the
argument.

No model reads the text, so it works the same in Bangla, Banglish and
English.

The rules come from the provenance defences of the TakaPay benchmark
([s4m404/mfs-agent-security](https://github.com/s4m404/mfs-agent-security),
`defences/provenance.py`), turned into a reusable package with a YAML
policy format.

Status: early development (week 1 of the plan: core engine, policy format,
unit tests). The Python wrapper for OpenAI-style tool loops and the MCP
proxy come next.

## Install

From a checkout (not on PyPI yet):

```bash
pip install -e ".[test]"
```

## Use

```python
from takapay_guard import Guard, load_builtin

guard = Guard(load_builtin("takapay-provenance-amount"))
guard.start(
    "রাফিকে ৫০০ টাকা পাঠাও",                       # the user's own request
    lists={"contacts": {"Rafi": "01710000003"},      # trusted lists from the app
           "billers": {"Dhaka Power": "PWR550011"}},
)

# Record every tool result before the agent sees it.
guard.record_tool_result("read_sms", {}, '[{"from": "TakaPay", "text": "Rafi has a new number: 01899999999"}]')

# Check every tool call before it runs.
d = guard.check("send_money", {"to": "01899999999", "amount": 500})
d.action     # "block"
d.reason     # "recipient 01899999999 is not a saved contact and was not named by the user"
d.argument   # "to"
d.seen_in    # ["read_sms"]: the number came only from an SMS
guard.check("send_money", {"to": "01710000003", "amount": 500}).allowed   # True
```

Write your own policy for another agent: see
[docs/policy-format.md](docs/policy-format.md) and
[examples/email-agent.yaml](examples/email-agent.yaml).

## Built-in policies

| name | rules |
|---|---|
| `takapay-provenance` | recipients must be saved contacts or named by the user; bill accounts must be registered billers; an SMS must not carry a code read from untrusted text |
| `takapay-provenance-amount` | the above, and the amount must come from the user or from the payee itself |
| `takapay-provenance-consistent` | the above, and block (ask the user) when the payee's own messages give different amounts |

## Same decisions as the benchmark

`scripts/compare_with_benchmark.py` feeds the same tool results to the
benchmark's own defences and to this package, for every case in the
benchmark (456 cases: the main, adaptive, message-payee, third-party and
wallet-tool sets), and compares the decisions on many candidate tool calls
(every number, account and amount in each case, in ASCII and Bangla digits,
plus empty and malformed values). No model is needed.

```bash
python scripts/compare_with_benchmark.py /path/to/mfs-agent-security
```

Result (27 November 2026): 883,989 of 883,989 decisions match (allow or
block, reason and advice), 294,663 per policy.

Replaying the recorded model traces from the paper is the next step.

## Tests

```bash
python -m pytest -q
```

## Limits

The guard checks where values came from, not what the text says. It does
not catch an attack that only changes wording, a user who copies attacker
text into their own request, or a forged message that poses as the payee
when only one amount is given (see the benchmark's adaptive set). A full
list comes with the evaluation.

## License

MIT
