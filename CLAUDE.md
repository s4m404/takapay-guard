# CLAUDE.md: brief for Claude sessions on this repo

## What this project is

takapay-guard (working name): a small open-source Python package that protects tool-using AI agents. Before an agent runs a sensitive tool call (send money, send email, delete a file), it checks where each important argument came from. If a payee, amount, address or code appears only in untrusted text (a tool result, a web page, a retrieved document) and never in the user's own request, it blocks the call or asks the user, with a clear reason. No model reads the text, so it works the same in Bangla, Banglish and English.

It turns the provenance defences from the TakaPay benchmark (repo s4m404/mfs-agent-security, file `defences/provenance.py`) into a reusable tool. Read that repo for the original logic; do not change it.

Owner: Mohammed Sayed Sameer (GitHub s4m404). He reviews and merges every pull request.

## How to work

- Write summaries in plain, short English; spell out any git or terminal step he must do himself.
- Keep everything tested: run the test suite before every push.
- Never invent results. Numbers in the README must come from real runs.
- Push work to a branch and open a pull request with a clear summary.

## Minimum version

1. Core library: a policy engine that records the user's request and every tool result, then checks each sensitive tool call against a policy.
2. Policy file (YAML): which tools are sensitive, which arguments must be trusted, and the action (block, ask the user, allow).
3. Two ways to use it:
   - a Python wrapper for an OpenAI-compatible tool-calling loop;
   - an MCP proxy that sits between any MCP client and server and checks tools/call requests.
4. Clear decision messages (in Bangla and English) that name the argument, where it came from and why it was blocked.
5. Tests, docs, a pip-installable package, and a short demo (GIF or small web page).

## Evaluation

- Replay the recorded TakaPay traces through the package: it should match the paper's numbers (0 attacks through with provenance and the amount check, 0 correct payments blocked).
- A second, different domain (for example a small email or file agent) to show it is not TakaPay-specific.
- Overhead: added time per tool call.
- Honest limits: what it cannot catch (for example attacks that only change wording, or a user who copies attacker text into the request).

## Related work (check current versions before comparing)

CaMeL and FIDES (research ideas the defence builds on); open-source guardrail tools such as Meta's LlamaFirewall and Invariant Labs' guardrails. Position: a small, rule-based, language-independent check on where tool arguments came from, not a text classifier.

## Plan (about 4 to 6 hours a week)

1. Week 1: package structure, YAML policy format, provenance logic moved into the package, unit tests.
2. Week 2: Python wrapper; replay TakaPay traces and match the paper's numbers.
3. Week 3: MCP proxy; run one real MCP server through it.
4. Week 4: second-domain demo, README, decision messages, demo GIF.
5. Then: publish to PyPI and link it from the TakaPay repo.
