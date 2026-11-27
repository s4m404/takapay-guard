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

Status: early development. Done: the core engine, the policy format, the
wrapper for OpenAI-style tool loops, and a replay of every recorded
benchmark run. Next: an MCP proxy.

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

### In an OpenAI-style tool-calling loop

`GuardedTools` wraps your tool functions: each call is checked before it
runs, a blocked call is not run (the model gets the reason instead), and
each result is recorded before the model sees it. It works with any client
that has `client.chat.completions.create(...)` (OpenAI, vLLM, Groq, ...);
the package itself does not import `openai`.

```python
from openai import OpenAI
from takapay_guard import Guard, GuardedTools, load_builtin, run_openai_loop

tools = GuardedTools(Guard(load_builtin("takapay-provenance-amount")),
                     {"read_sms": read_sms, "send_money": send_money},   # your functions
                     ask_user=None)   # or a function that asks the user for "ask" decisions
tools.start(user_request, lists={"contacts": contacts, "billers": billers})
messages = [{"role": "user", "content": user_request}]
answer = run_openai_loop(OpenAI(), "gpt-4o-mini", messages, tool_schemas, tools)
```

If you have your own loop, call `tools.handle(assistant_message)` for each
model reply: it returns the `{"role": "tool", ...}` messages to append.

### Your own policy

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

## Replay of the recorded benchmark runs

`scripts/replay_traces.py` replays every recorded model run of the
benchmark (all run folders on its `kaggle-results` and `groq-results`
branches) through the package. No model is run. Full report:
[docs/replay.md](docs/replay.md).

```bash
cd /path/to/mfs-agent-security
git fetch origin kaggle-results groq-results
git worktree add /tmp/kaggle-results origin/kaggle-results
git worktree add /tmp/groq-results origin/groq-results
cd /path/to/takapay-guard
python scripts/replay_traces.py /path/to/mfs-agent-security /tmp/kaggle-results /tmp/groq-results --out docs/replay.md
```

Results (27 November 2026):

- **Runs made with a provenance defence** (59 runs): the package gives the
  same decision as the benchmark's defence code on 17,617 of 17,617 logged
  tool calls (17,612 as logged; the 5 others are the benchmark's own known
  differences from rules it changed after those runs). So the runs'
  recorded outcomes hold for the package. On the main 302-case set with
  `takapay-provenance-amount` (12 runs, 2,640 attack episodes): **0
  attacks through and 0 correct payments blocked**, as in the paper.
- **Runs without a defence**, replayed up to the first call the package
  would block: of the 299 main-set attacks the models fell for, the
  package blocks the attacking call in 251; in the other 48 it blocks an
  earlier call first (for example an invented payee), and none would have
  gone through. In the 599 main-set normal tasks the models did, it blocks
  the correct payment in 0.
- **Overhead** (36,303 checks): `Guard.check` median 2.5 µs (95th
  percentile 99 µs); `Guard.record_tool_result` median 38 µs (95th
  percentile 310 µs), on one CPU core.

The known cost: a payment to a number found only in an SMS or invoice
(the benchmark's message-payee set) is always blocked, by design.

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
