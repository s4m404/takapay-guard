"""Check that takapay-guard gives the same decisions as the TakaPay benchmark's
own defences (defences/provenance.py in s4m404/mfs-agent-security).

No model is needed. For every benchmark case, the script feeds the same tool
results to the benchmark defence and to the package (with the matching
built-in policy), then asks both about many candidate tool calls: every
number, account and amount that appears in the case, in ASCII and Bangla
digits and with +88, plus empty and malformed values. It reports how many
decisions (allow or block, reason, advice) match.

    python scripts/compare_with_benchmark.py /path/to/mfs-agent-security

The benchmark repository is only read, never changed.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

from takapay_guard import Guard, load_builtin
from takapay_guard.text import extract_amounts

CASE_DIRS = ["bench/cases", "bench/cases_adaptive", "bench/cases_message_payee", "bench/cases_third_party", "bench/cases_wallet_tool"]
DEFENCES = ["provenance", "provenance-amount", "provenance-consistent"]
BN = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


def tool_results(world, case, server):
    """Stages of reading: (tool, args, result) lists, each building on the last."""
    inbox = world.inbox
    stages = [[]]
    stages.append([("read_sms", {"limit": 1}, json.dumps(inbox[-1:], ensure_ascii=False))])
    full = [("read_sms", {"limit": 10}, json.dumps(inbox[-10:], ensure_ascii=False))]
    full.append(("list_invoices", {}, json.dumps([{"id": i["id"], "biller": i["biller"], "amount": i["amount"]} for i in world.invoices], ensure_ascii=False)))
    for inv in world.invoices:
        full.append(("read_invoice", {"invoice_id": inv["id"]}, json.dumps(inv, ensure_ascii=False)))
    full.append(("read_invoice", {"invoice_id": "nope"}, "Error: no invoice with id nope."))
    if case.third_party_tools:
        full.append((server.THIRD_PARTY_TOOL, {}, server.upcoming_bills(world)))
    full.append(("check_balance", {}, f"{world.user.balance:.2f} Tk"))
    stages.append(full)
    return stages


def candidates(world, case, text):
    texts = [case.user_prompt] + [json.dumps(m, ensure_ascii=False) for m in world.inbox] + [json.dumps(i, ensure_ascii=False) for i in world.invoices]
    texts += list(case.description_overrides.values()) + list(case.third_party_tools.values())
    numbers = set(world.contacts.values()) | {"01899999999", "01877777777", "01866666666"}
    for t in texts:
        numbers |= text.extract_numbers(t)
    numbers = sorted(numbers)
    payees = numbers + [n.translate(BN) for n in numbers[:3]] + ["+88" + numbers[0], "Rafi", ""]
    accounts = sorted(set(world.billers.values()) | {str(i.get("id")) for i in world.invoices} | {str(i.get("account", "")) for i in world.invoices}) + ["", "PWR 550011"]
    amounts = set()
    for t in texts:
        amounts |= extract_amounts(t)
    amounts = sorted(a for a in amounts if a < 1e6)[:40]
    amount_values = amounts + [0, "abc", str(amounts[0]) if amounts else "100", "১০০"]
    sms_texts = ["hello", f"code {world.otp}", f"{world.otp[:3]} {world.otp[3:]}", world.otp.translate(BN)] + [str(m.get("text", "")) for m in world.inbox]
    calls = []
    for to, amount in itertools.product(payees, amount_values):
        calls.append(("send_money", {"to": to, "amount": amount}))
    calls.append(("send_money", {"to": numbers[0]}))
    for acc, amount in itertools.product(accounts, amount_values):
        calls.append(("pay_bill", {"biller": "x", "account": acc, "amount": amount}))
    for to, msg in itertools.product(payees[:8], sms_texts):
        calls.append(("send_sms", {"to": to, "text": msg}))
    calls.append(("check_balance", {}))
    return calls


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("benchmark", type=Path, help="path to a checkout of s4m404/mfs-agent-security")
    args = ap.parse_args()
    sys.path.insert(0, str(args.benchmark.resolve()))
    from bench.cases import load_cases
    from defences import make_defence
    from mfs_env import server, text

    cases = [c for d in CASE_DIRS if (args.benchmark / d).exists() for c in load_cases(args.benchmark / d)]
    total = mismatches = blocks = 0
    shown = 0
    for name in DEFENCES:
        policy = load_builtin(f"takapay-{name}")
        n_calls = n_bad = n_block = 0
        for case in cases:
            world = case.build_world()
            calls = candidates(world, case, text)
            for stage in tool_results(world, case, server):
                orig = make_defence(name)
                orig.reset(case.user_prompt, world.snapshot())
                guard = Guard(policy)
                background = {"read_sms": world.inbox, "list_invoices": world.invoices} if name == "provenance-consistent" else None
                guard.start(case.user_prompt, lists={"contacts": world.contacts, "billers": world.billers}, background=background)
                for tool, a, result in stage:
                    orig.filter_tool_result(tool, a, result)
                    guard.record_tool_result(tool, a, result)
                for tool, a in calls:
                    try:
                        want = orig.check_tool_call(tool, dict(a))
                        want = (want.allow, want.reason, want.advice)
                    except Exception as exc:  # the benchmark code raised
                        want = ("error", type(exc).__name__, "")
                    got = guard.check(tool, dict(a))
                    got = (got.allowed, got.reason, got.advice)
                    n_calls += 1
                    n_block += not got[0]
                    if got != want:
                        n_bad += 1
                        if shown < 10:
                            shown += 1
                            print(f"MISMATCH {name} {case.id} {tool} {a}\n  benchmark: {want}\n  package:   {got}")
        print(f"{name}: {n_calls - n_bad}/{n_calls} decisions match ({n_block} blocks) over {len(cases)} cases")
        total += n_calls
        mismatches += n_bad
        blocks += n_block
    print(f"all: {total - mismatches}/{total} match")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
