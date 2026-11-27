"""The policy engine: record what the user asked and what each tool
returned, then check every sensitive tool call against the policy.

Typical use, once per user task::

    guard = Guard(load_builtin("takapay-provenance-amount"))
    guard.start(user_request, lists={"contacts": contacts, "billers": billers})
    ...
    decision = guard.check("send_money", {"to": "01899999999", "amount": 500})
    if not decision.allowed:
        ...  # do not run the call; show decision.reason to the agent or user
    result = run_tool(...)
    guard.record_tool_result(tool, args, result)

No model reads any text: every check is a string rule, so it behaves the
same in Bangla, Banglish and English.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .policy import LIST_PREFIX, SOURCE_PAYEE, SOURCE_USER, ArgumentRule, Policy, SourceRule
from .text import extract_amounts, extract_numbers, join_digit_groups, normalise_number, normalise_text, to_ascii_digits

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# An account number such as "PWR550011" or "GAS-770022" (letters keep their case).
_ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{3,}")


@dataclass
class Decision:
    """The answer for one tool call.

    ``action`` is ``"allow"``, ``"block"`` or ``"ask"`` (ask the user before
    running the call). When a check failed, ``rule`` names it
    (``untrusted_value``, ``invalid_value``, ``inconsistent_amount``,
    ``untrusted_code``) and ``argument`` / ``value`` say what failed.
    ``seen_in`` lists the untrusted tools whose results contained the value
    (empty: the value appeared nowhere, for example an invented number).
    A failed check with action ``allow`` (monitor mode) keeps ``rule`` set.
    """

    action: str = "allow"
    reason: str = ""
    # What the agent should do next. Empty means the host's default
    # (in the TakaPay benchmark: ask the user for explicit approval).
    advice: str = ""
    tool: str = ""
    argument: str = ""
    value: Any = None
    rule: str = ""
    seen_in: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


def _items(result: Any) -> list[Any] | None:
    """The JSON items in a tool result, or None when it is not JSON."""
    if isinstance(result, (list, dict)):
        data = result
    else:
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            return None
    return data if isinstance(data, list) else [data]


def _texts(result: Any) -> list[str]:
    """The separate texts in a tool result: each SMS or invoice on its own."""
    items = _items(result)
    if items is None:
        return [str(result)]
    return [json.dumps(i, ensure_ascii=False) if not isinstance(i, str) else i for i in items]


def _as_text(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class Guard:
    """Provenance checks for one user task (one agent episode)."""

    def __init__(self, policy: Policy):
        self.policy = policy
        self.start("")

    # ------------------------------------------------------------ recording

    def start(
        self,
        user_request: str,
        lists: Mapping[str, Iterable[str] | Mapping[str, str]] | None = None,
        background: Mapping[str, Any] | None = None,
    ) -> None:
        """Begin a new task.

        ``user_request``: the user's own words (trusted).
        ``lists``: trusted lists named in the policy (``list:<name>``), each a
        list of values or a mapping of name to value (contacts, billers).
        ``background``: untrusted items the host holds but the agent may not
        have read, by tool name (for example the whole SMS inbox under
        ``read_sms``). They are used only by the consistency check.
        """
        self.user_request = user_request
        self._list_values: dict[str, set[str]] = {}
        self._name_maps: dict[str, dict[str, str]] = {}
        for name, values in (lists or {}).items():
            if isinstance(values, Mapping):
                self._name_maps[name] = {normalise_text(str(k)): normalise_number(str(v)) for k, v in values.items()}
                values = values.values()
            self._list_values[name] = {str(v) for v in values}
        self._user_cache: dict[str, set[Any]] = {}
        self.untrusted_codes: set[str] = set()
        self._code_origin: dict[str, str] = {}
        self._payee_amounts: dict[str, set[float]] = {}
        self._consistency: dict[str, dict[str, set[float]]] = {}
        self._seen: list[tuple[str, str]] = []
        self.log: list[dict[str, Any]] = []
        for tool, items in (background or {}).items():
            for rule_id, rule in enumerate(self.policy.sources):
                if tool in rule.tools:
                    for item in _items(items) or []:
                        if isinstance(item, dict):
                            self._add_consistency(rule_id, rule, item)

    def record_user_message(self, text: str) -> None:
        """Add a later user message to the trusted request."""
        self.user_request = f"{self.user_request}\n{text}" if self.user_request else text
        self._user_cache.clear()

    def record_tool_result(self, tool: str, args: Mapping[str, Any] | None, result: Any) -> None:
        """Record a tool result before the agent sees it. Results of tools
        not listed in ``untrusted_tools`` carry no untrusted values."""
        if tool not in self.policy.untrusted_tools:
            return
        texts = _texts(result)
        for text in texts:
            norm = normalise_text(text)
            self._seen.append((tool, text))
            codes = self.policy.codes
            if codes is not None and codes.context.search(norm):
                for code in codes.pattern.findall(norm):
                    self.untrusted_codes.add(code)
                    self._code_origin.setdefault(code, tool)
        items = _items(result)
        if items is None:
            return
        for rule_id, rule in enumerate(self.policy.sources):
            if tool not in rule.tools:
                continue
            for item in items:
                if isinstance(item, dict):
                    payee = self._payee(rule, item)
                    if payee:
                        self._payee_amounts.setdefault(payee, set()).update(extract_amounts(str(item.get(rule.amount_field, ""))))
                    self._add_consistency(rule_id, rule, item)

    def _payee(self, rule: SourceRule, item: Mapping[str, Any]) -> str:
        payee = normalise_number(str(item.get(rule.payee_field, ""))) if rule.payee_field else ""
        if not payee and rule.payee_name_field:
            name = normalise_text(str(item.get(rule.payee_name_field, "")))
            payee = self._name_maps.get(rule.payee_name_list, {}).get(name, "")
        return payee

    def _add_consistency(self, rule_id: int, rule: SourceRule, item: Mapping[str, Any]) -> None:
        payee = self._payee(rule, item)
        amounts = extract_amounts(str(item.get(rule.amount_field, "")))
        limit = self.policy.consistency_ignore_above
        if limit is not None:
            # Values this large are phone or account numbers, not amounts.
            amounts = {a for a in amounts if a < limit}
        if payee and amounts:
            ident = next((str(item[f]) for f in rule.id_fields if item.get(f)), "")
            self._consistency.setdefault(payee, {}).setdefault(f"{rule_id}:{ident}", set()).update(amounts)

    # ------------------------------------------------------------ checking

    def check(self, tool: str, args: Mapping[str, Any] | None) -> Decision:
        """Allow, block or ask for one tool call, before it runs."""
        args = dict(args or {})
        tool_rule = self.policy.tools.get(tool)
        if tool_rule is None:
            return self._done(tool, args, Decision("allow", tool=tool))
        flagged: Decision | None = None
        for rule in tool_rule.arguments:
            failure = self._check_argument(tool, rule, args)
            if failure is None:
                continue
            if failure.action != "allow":
                return self._done(tool, args, failure)
            flagged = flagged or failure  # monitor mode: note it, keep checking
        return self._done(tool, args, flagged or Decision("allow", tool=tool))

    def _done(self, tool: str, args: dict[str, Any], decision: Decision) -> Decision:
        self.log.append({"tool": tool, "args": args, "action": decision.action, "rule": decision.rule, "reason": decision.reason})
        return decision

    def _fail(self, tool: str, rule: ArgumentRule, kind: str, value: Any, reason: str, advice: str) -> Decision:
        return Decision(rule.action, reason, advice, tool, rule.name, value, kind, self._seen_in(rule, value))

    def _check_argument(self, tool: str, rule: ArgumentRule, args: dict[str, Any]) -> Decision | None:
        raw = args.get(rule.name, 0 if rule.type == "amount" else "")
        if rule.type == "text":
            return self._check_codes(tool, rule, raw)

        if rule.type == "amount":
            try:
                value: Any = round(float(raw), 2)
            except (TypeError, ValueError):
                msg = rule.invalid_message or "{argument} is not a number"
                return self._fail(tool, rule, "invalid_value", raw, self._format(msg, rule, raw, args), rule.advice)
        else:
            value = _normalise(rule.type, raw)

        payee = normalise_number(str(args.get(rule.payee, ""))) if rule.payee else ""
        if rule.trusted_from and not self._trusted(rule, value, payee):
            msg = rule.message or "{argument} {value} was not given by the user or a trusted list"
            return self._fail(tool, rule, "untrusted_value", value, self._format(msg, rule, value, args), rule.advice)
        if rule.consistent and not (SOURCE_USER in rule.trusted_from and value in self._from_user(rule.type)):
            others = sorted(
                {a for amounts in self._consistency.get(payee, {}).values() if value not in amounts for a in amounts}
            )
            if others:
                msg = rule.consistent_message or "the payee {payee} gave different amounts ({value:g} and {others}) in different messages"
                return self._fail(
                    tool,
                    rule,
                    "inconsistent_amount",
                    value,
                    self._format(msg, rule, value, args, others=", ".join(f"{a:g}" for a in others[:3])),
                    rule.consistent_advice,
                )
        if rule.no_untrusted_codes:
            return self._check_codes(tool, rule, raw)
        return None

    def _check_codes(self, tool: str, rule: ArgumentRule, raw: Any) -> Decision | None:
        joined = join_digit_groups(str(raw))
        for code in sorted(self.untrusted_codes):
            if code in joined:
                msg = rule.message or "{argument} contains a code read from untrusted content"
                d = self._fail(tool, rule, "untrusted_code", code, self._format(msg, rule, code, {}), rule.advice)
                d.seen_in = [self._code_origin[code]]
                return d
        return None

    def _trusted(self, rule: ArgumentRule, value: Any, payee: str) -> bool:
        for src in rule.trusted_from:
            if src == SOURCE_USER and value in self._from_user(rule.type):
                return True
            if src == SOURCE_PAYEE and value in self._payee_amounts.get(payee, set()):
                return True
            if src.startswith(LIST_PREFIX) and value in self._list(src[len(LIST_PREFIX) :], rule.type):
                return True
        return False

    def _list(self, name: str, vtype: str) -> set[Any]:
        values = self._list_values.get(name, set())
        if vtype == "amount":
            out = set()
            for v in values:
                try:
                    out.add(round(float(v), 2))
                except ValueError:
                    pass
            return out
        return {_normalise(vtype, v) for v in values}

    def _from_user(self, vtype: str) -> set[Any]:
        if vtype not in self._user_cache:
            self._user_cache[vtype] = _extract(vtype, self.user_request)
        return self._user_cache[vtype]

    def _seen_in(self, rule: ArgumentRule, value: Any) -> list[str]:
        if rule.type == "text":
            return []
        tools = []
        for tool, text in self._seen:
            if tool not in tools and value in _extract(rule.type, text):
                tools.append(tool)
        return tools

    @staticmethod
    def _format(template: str, rule: ArgumentRule, value: Any, args: Mapping[str, Any], others: str = "") -> str:
        fields = {
            "argument": rule.name,
            "value": value,
            "payee": normalise_number(str(args.get(rule.payee, ""))) if rule.payee else "",
            "others": others,
        }
        try:
            return template.format(**fields)
        except (KeyError, IndexError, ValueError):
            return template


def _normalise(vtype: str, value: Any) -> str:
    """One value of a phone, account or email argument, in a form to compare."""
    if vtype == "email":
        return str(value).strip().lower()
    return normalise_number(str(value))


def _extract(vtype: str, text: str) -> set[Any]:
    """The values of one type that appear in a text."""
    if vtype == "phone":
        return extract_numbers(text)
    if vtype == "amount":
        return extract_amounts(text)
    if vtype == "email":
        return {m.lower() for m in _EMAIL_RE.findall(text)}
    if vtype == "account":
        # Account numbers such as "PWR550011" or "GAS-770022", in any script's digits.
        return extract_numbers(text) | {normalise_number(m) for m in _ACCOUNT_RE.findall(to_ascii_digits(text))}
    return set()
