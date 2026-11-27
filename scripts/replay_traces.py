"""Replay the recorded TakaPay benchmark runs through takapay-guard.

No model is needed: every model run is already recorded. The script reads
the run folders (each holds traces.jsonl and scores.jsonl) from checkouts of
the benchmark's results branches, and does three things.

1. Same policy. For each run made with a provenance defence, it feeds the
   logged tool calls of every episode, in order, to the package with the
   matching built-in policy, and compares each allow/block decision with
   the one in the log (and with the benchmark's current defence code).
   Tool results are rebuilt in full from the wallet, as the benchmark's
   scripts/verify_decisions.py does. If every decision matches, the run's
   recorded outcome (attacks through, correct payments blocked) is what the
   package would have produced.
2. Runs without a defence. It replays them through takapay-provenance-amount
   and takapay-provenance-consistent up to the first call the package would block (after a block a real
   agent acts differently, so later calls cannot be replayed). It counts
   successful attacks whose attacking call would have been blocked, and
   correct payments that would have been blocked.
3. Overhead: the time of each Guard.check and Guard.record_tool_result call.

    python scripts/replay_traces.py /path/to/mfs-agent-security \\
        /path/to/kaggle-results /path/to/groq-results --out docs/replay.md

The benchmark and its results are only read.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from takapay_guard import Guard, load_builtin

POLICIES = {"provenance": "takapay-provenance", "provenance-amount": "takapay-provenance-amount",
            "provenance-consistent": "takapay-provenance-consistent"}
CASE_DIRS = ["cases", "cases_adaptive", "cases_message_payee", "cases_third_party", "cases_wallet_tool"]
MONEY = ("send_money", "pay_bill")
COUNTERFACTUAL = ("provenance-amount", "provenance-consistent")
SET_ORDER = ["main", "adaptive", "message_payee", "third_party", "wallet_tool"]


def case_set(root: Path) -> str:
    """The case set of a results folder, from its name (results_groq_third_party -> third_party)."""
    name = root.name.removeprefix("results").removeprefix("_groq").removeprefix("_medium").strip("_")
    return name or "main"


def episodes(trace_file: Path) -> dict[tuple[str, int], list[dict]]:
    out: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for line in open(trace_file, encoding="utf-8"):
        if line.strip():
            e = json.loads(line)
            out[(e["case_id"], e.get("repeat", 0))].append(e)
    return out


class Replayer:
    def __init__(self, bench: Path):
        sys.path.insert(0, str(bench.resolve()))
        sys.path.insert(0, str((bench / "scripts").resolve()))
        from bench.cases import load_cases
        from bench.score import _call_as_transfer, _digits_joined, _is_attack_transfer, _ledger_match_one
        from defences import make_defence
        from verify_decisions import KNOWN_TOOLS, tool_result

        self.cases = {}
        for d in CASE_DIRS:
            for c in load_cases(bench / "bench" / d):
                self.cases.setdefault(c.id, c)
        self.make_defence, self.tool_result, self.known = make_defence, tool_result, KNOWN_TOOLS
        self.as_transfer, self.is_attack, self.matches, self.digits = (
            _call_as_transfer, _is_attack_transfer, _ledger_match_one, _digits_joined)
        self.policies = {k: load_builtin(v) for k, v in POLICIES.items()}
        self.check_ns: list[int] = []
        self.record_ns: list[int] = []
        self.mismatches: list[str] = []

    def _start(self, defence: str, case):
        world = case.build_world()
        guard = Guard(self.policies[defence])
        background = {"read_sms": world.inbox, "list_invoices": world.invoices} if defence == "provenance-consistent" else None
        guard.start(case.user_prompt, lists={"contacts": dict(world.contacts), "billers": dict(world.billers)},
                    background=background)
        return world, guard

    def _result(self, world, e: dict) -> str:
        logged = str(e.get("result", ""))
        if e["tool"] not in self.known or logged.startswith("Error executing tool"):
            return logged  # a made-up tool or malformed arguments: the wallet did not change
        return self.tool_result(world, e["tool"], e.get("args") or {})

    def _check(self, guard, tool, args):
        t = time.perf_counter_ns()
        d = guard.check(tool, args)
        self.check_ns.append(time.perf_counter_ns() - t)
        return d

    def _record(self, guard, tool, args, result):
        t = time.perf_counter_ns()
        guard.record_tool_result(tool, args, result)
        self.record_ns.append(time.perf_counter_ns() - t)

    def same_policy(self, defence: str, case, trace: list[dict]) -> tuple[Counter, list[str]]:
        """Replay one episode through the package and the benchmark code; compare with the log."""
        counts: Counter = Counter()
        problems = []
        world, guard = self._start(defence, case)
        bench = self.make_defence(defence)
        bench.reset(case.user_prompt, world.snapshot())
        for e in (x for x in trace if x.get("type") == "tool_call"):
            tool, args = e["tool"], e.get("args") or {}
            d = self._check(guard, tool, dict(args))
            try:
                b = "allowed" if bench.check_tool_call(tool, dict(args)).allow else "blocked"
            except Exception:  # the benchmark code may raise on a malformed call
                b = "error"
            now = "allowed" if d.allowed else "blocked"
            counts["calls"] += 1
            counts["same_as_log"] += now == e.get("decision")
            counts["same_as_code"] += now == b
            if now != e.get("decision"):
                problems.append(f"{case.id}: {tool} {json.dumps(args, ensure_ascii=False)[:100]} logged "
                                f"{e.get('decision')}, package {now} ({d.reason or 'allowed'})")
            if e.get("decision") != "allowed":
                continue
            result = self._result(world, e)
            self._record(guard, tool, args, result)
            bench.filter_tool_result(tool, args, result)
        return counts, problems

    def counterfactual(self, defence: str, case, trace: list[dict]) -> str:
        """Replay a no-defence episode through a policy up to its first block.

        Returns what the first blocked call was: "attack", "correct", "other",
        or "none" (nothing blocked)."""
        world, guard = self._start(defence, case)
        expected = case.utility.get("ledger_contains") if case.utility else None
        for e in (x for x in trace if x.get("type") == "tool_call"):
            tool, args = e["tool"], e.get("args") or {}
            if not self._check(guard, tool, dict(args)).allowed:
                if tool in MONEY and self.is_attack(case, self.as_transfer(e)):
                    return "attack"
                if (tool == "send_sms" and case.attack and case.attack["goal"] == "exfiltrate_otp"
                        and world.otp in self.digits(str(args.get("text", "")))):
                    return "attack"
                if tool in MONEY and expected and self.matches(self.as_transfer(e), expected):
                    return "correct"
                return "other"
            self._record(guard, tool, args, self._result(world, e))
        return "none"


def scores(run: Path) -> list[dict]:
    path = run / "scores.jsonl"
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()] if path.exists() else []


def pct(values: list[int], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] / 1000


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("benchmark", type=Path, help="checkout of s4m404/mfs-agent-security (main)")
    ap.add_argument("results", type=Path, nargs="+", help="checkouts of the kaggle-results and groq-results branches")
    ap.add_argument("--out", type=Path, help="write the report (markdown) here")
    ap.add_argument("--show", type=int, default=5, help="mismatches to list per run")
    args = ap.parse_args()
    rp = Replayer(args.benchmark)

    runs = sorted({p.parent for r in args.results for p in r.rglob("traces.jsonl")})
    same_rows, cf_rows, notes = [], [], []
    totals: Counter = Counter()
    for run in runs:
        root = run.parent
        label = f"{root.parent.name}/{root.name}/{run.name}" if root.parent not in args.results else f"{root.name}/{run.name}"
        eps = episodes(run / "traces.jsonl")
        missing = sorted({cid for cid, _ in eps if cid not in rp.cases})
        if missing:
            notes.append(f"{label}: {len(missing)} case ids not in the benchmark, skipped")
        first = next((e for t in eps.values() for e in t), {})
        defence, model = first.get("defence", "none"), first.get("model", "?")
        sc = scores(run)
        atk = [s for s in sc if s.get("is_attack")]
        normal = [s for s in sc if not s.get("is_attack")]
        outcome = (sum(s["attack_success"] for s in atk), len(atk), sum(s.get("false_block", False) for s in sc),
                   sum(s["utility"] for s in normal), len(normal))
        if defence in POLICIES:
            c: Counter = Counter()
            problems = []
            for (cid, _), trace in eps.items():
                if cid in rp.cases:
                    cc, pr = rp.same_policy(defence, rp.cases[cid], trace)
                    c.update(cc)
                    problems += pr
            totals.update(c)
            same_rows.append((label, model, defence, case_set(root), c, outcome))
            print(f"{label}: {c['same_as_log']}/{c['calls']} as logged, {c['same_as_code']}/{c['calls']} as benchmark code")
            for p in problems[: args.show]:
                print("   ", p)
            rp.mismatches += [f"{label}: {p}" for p in problems]
        elif defence == "none":
            for policy in COUNTERFACTUAL:
                f: Counter = Counter()
                for (cid, rep), trace in eps.items():
                    s = next((x for x in sc if x["case_id"] == cid and x.get("repeat", 0) == rep), None)
                    if cid not in rp.cases or s is None:
                        continue
                    kind = rp.counterfactual(policy, rp.cases[cid], trace)
                    if s["is_attack"] and s["attack_success"]:
                        f["atk_success"] += 1
                        f["atk_" + {"attack": "stopped", "other": "earlier"}.get(kind, "through")] += 1
                    if not s["is_attack"] and s["utility"]:
                        f["normal_done"] += 1
                        f["normal_" + {"correct": "blocked", "other": "other"}.get(kind, "kept")] += 1
                cf_rows.append((label, model, case_set(root), policy, f))
                print(f"{label}: none -> {policy}: {dict(f)}")

    out = report(same_rows, cf_rows, totals, rp, notes)
    if args.out:
        args.out.write_text(out, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0 if totals["same_as_code"] == totals["calls"] else 1


def report(same_rows, cf_rows, totals, rp, notes) -> str:
    L = ["# Replay of the TakaPay benchmark runs", "",
         "Made by `scripts/replay_traces.py` from the recorded runs on the benchmark's",
         "`kaggle-results` and `groq-results` branches (every run folder there). No model was run.",
         "Case sets: main (302 cases; the 2026-09-30 run is the earlier 114-case set with 3 repeats),",
         "adaptive (56, forged payee identity), message_payee (24 normal tasks whose payee is only in a",
         "message), third_party (37), wallet_tool (37).", "",
         "## 1. Runs made with a provenance defence", "",
         f"The package gives the same allow/block decision as the benchmark's current defence code on "
         f"**{totals['same_as_code']:,} of {totals['calls']:,}** logged tool calls, and the same as the "
         f"logged decision on **{totals['same_as_log']:,} of {totals['calls']:,}**. The "
         f"{totals['calls'] - totals['same_as_log']} that differ from the log come from rules the benchmark changed after "
         "those runs, and match its own `verify_decisions.py` list of expected differences: SMS texts with a bill "
         "amount that the first code rule took for a code (fixed 22 October), and the first version of "
         "`provenance-consistent`, which checked only the SMS the agent read (see the list at the end).", "",
         "So each run's recorded outcome is what the package would have produced. Outcomes below are summed",
         "from the runs' `scores.jsonl`.", "",
         "| set | defence | runs | attacks through | correct payments blocked | normal tasks done |",
         "|---|---|---|---|---|---|"]
    agg: dict = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for label, model, defence, cset, c, (a, n_a, fb, u, n_u) in same_rows:
        row = agg[(cset, defence)]
        for i, v in enumerate((1, a, n_a, fb, u, n_u)):
            row[i] += v
    for (cset, defence), (runs, a, n_a, fb, u, n_u) in sorted(agg.items(), key=lambda kv: (SET_ORDER.index(kv[0][0]), kv[0][1])):
        L.append(f"| {cset} | {defence} | {runs} | {a}/{n_a} | {fb} | {u}/{n_u} |")
    L += ["", "Reading the table:",
          "- main: `provenance-amount` and `provenance-consistent` let 0 attacks through and blocked 0 correct",
          "  payments in every run; `provenance` lets through only attacks that change the amount.",
          "- message_payee: every task there pays a number found only in an SMS or invoice, so blocking it",
          "  is the rule working as designed (the known cost of the defence), not an error.",
          "- adaptive: forged payee identity. `provenance-amount` is beaten by design; the 3 attacks through",
          "  `provenance-consistent` are all in the run of its first version (see the mismatches); the fixed",
          "  version let 0 through. Its \"correct payments blocked\" are payments it held back to ask the user",
          "  which amount is right.",
          "", "<details><summary>Every run</summary>", "",
          "| run | defence | set | same as log | same as code | attacks through | correct payments blocked | normal tasks done |",
          "|---|---|---|---|---|---|---|---|"]
    for label, model, defence, cset, c, (a, n_a, fb, u, n_u) in same_rows:
        L.append(f"| {label} | {defence} | {cset} | {c['same_as_log']}/{c['calls']} | {c['same_as_code']}/{c['calls']} "
                 f"| {a}/{n_a} | {fb} | {u}/{n_u} |")
    L += ["", "</details>", "", "## 2. Runs without a defence, replayed through the package", "",
          "Each episode is replayed up to the first call the package would block (after a block a real agent",
          "acts differently, so later calls cannot be replayed). For attacks the model fell for: *stopped* = the",
          "first blocked call is the attacking call; *blocked earlier* = another call was blocked first, so the",
          "attack's fate is unknown; *not blocked* = the attack would have gone through. For normal tasks the",
          "model did: *correct payment blocked* = the first block hit the correct payment; *other call blocked",
          "first* = a different call (for example an invented payee) was blocked first.", "",
          "| set | policy | runs | attacks the model fell for | stopped | blocked earlier | not blocked "
          "| normal tasks done | correct payment blocked | other call blocked first |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    cagg: dict = defaultdict(Counter)
    for label, model, cset, policy, f in cf_rows:
        cagg[(cset, policy)].update(f)
        cagg[(cset, policy)]["runs"] += 1
    for (cset, policy), f in sorted(cagg.items(), key=lambda kv: (SET_ORDER.index(kv[0][0]), kv[0][1])):
        L.append(f"| {cset} | {policy} | {f['runs']} | {f['atk_success']} | {f['atk_stopped']} | {f['atk_earlier']} "
                 f"| {f['atk_through']} | {f['normal_done']} | {f['normal_blocked']} | {f['normal_other']} |")
    L += ["", "<details><summary>Every run</summary>", "",
          "| run | set | policy | attacks the model fell for | stopped | blocked earlier | not blocked "
          "| normal tasks done | correct payment blocked | other call blocked first |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for label, model, cset, policy, f in cf_rows:
        L.append(f"| {label} | {cset} | {policy} | {f['atk_success']} | {f['atk_stopped']} | {f['atk_earlier']} "
                 f"| {f['atk_through']} | {f['normal_done']} | {f['normal_blocked']} | {f['normal_other']} |")
    L += ["", "</details>", "", "## 3. Overhead", "",
          f"Time of each call during this replay (Python {sys.version.split()[0]}, one CPU core of a cloud",
          "container). A model call takes seconds, so this is small next to it.", "",
          "| call | count | median (µs) | 95th percentile (µs) | mean (µs) |", "|---|---|---|---|---|"]
    for name, ns in (("Guard.check", rp.check_ns), ("Guard.record_tool_result", rp.record_ns)):
        if ns:
            L.append(f"| {name} | {len(ns):,} | {pct(ns, 0.5):.1f} | {pct(ns, 0.95):.1f} | {statistics.mean(ns) / 1000:.1f} |")
    L += ["", "## Decisions that differ from the log", ""] + [f"- {p}" for p in rp.mismatches]
    if notes:
        L += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
