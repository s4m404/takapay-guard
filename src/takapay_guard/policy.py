"""The policy format: which tools are sensitive, which arguments must be
trusted, where trust can come from, and what to do when a check fails.

A policy is written in YAML (see ``docs/policy-format.md``) and loaded with
:func:`load_policy` or :func:`load_builtin`. Loading checks every key, so a
typo fails loudly instead of silently turning a check off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

FORMAT_VERSION = 1
ACTIONS = ("block", "ask", "allow")
VALUE_TYPES = ("phone", "account", "email", "amount", "text")
# Where a trusted value may come from.
#   user_request   the user's own request (the first user message)
#   list:<name>    a trusted list the host passes to Guard.start (contacts, billers)
#   payee_sources  an untrusted result attributed to the payee itself (amounts only)
SOURCE_USER = "user_request"
SOURCE_PAYEE = "payee_sources"
LIST_PREFIX = "list:"


class PolicyError(ValueError):
    """The policy file is not valid."""


@dataclass(frozen=True)
class CodeRule:
    """How to find secret codes (OTP, PIN) in untrusted text."""

    pattern: re.Pattern[str]
    context: re.Pattern[str]


@dataclass(frozen=True)
class SourceRule:
    """How to read payee-attributed amounts from one kind of untrusted item.

    For every JSON object in a result of ``tools``, the payee is
    ``payee_field`` (or, if that is empty, ``payee_name_field`` looked up in
    the trusted list ``payee_name_list``), and the amounts are the numbers in
    ``amount_field``.
    """

    tools: tuple[str, ...]
    amount_field: str
    payee_field: str = ""
    payee_name_field: str = ""
    payee_name_list: str = ""
    id_fields: tuple[str, ...] = ("id",)


@dataclass(frozen=True)
class ArgumentRule:
    name: str
    type: str
    trusted_from: tuple[str, ...] = ()
    payee: str = ""
    consistent: bool = False
    no_untrusted_codes: bool = False
    action: str = "block"
    message: str = ""
    advice: str = ""
    invalid_message: str = ""
    consistent_message: str = ""
    consistent_advice: str = ""


@dataclass(frozen=True)
class ToolRule:
    name: str
    arguments: tuple[ArgumentRule, ...]


@dataclass(frozen=True)
class Policy:
    name: str
    description: str = ""
    untrusted_tools: frozenset[str] = frozenset()
    codes: CodeRule | None = None
    sources: tuple[SourceRule, ...] = ()
    consistency_ignore_above: float | None = None
    tools: dict[str, ToolRule] = field(default_factory=dict)

    @property
    def sensitive_tools(self) -> frozenset[str]:
        return frozenset(self.tools)

    @classmethod
    def from_dict(cls, data: Any) -> "Policy":
        return _parse_policy(data)


# ---------------------------------------------------------------- loading


def load_policy(source: str | Path) -> Policy:
    """Load a policy from a YAML file path, or from YAML text."""
    if isinstance(source, Path) or ("\n" not in str(source) and Path(str(source)).exists()):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = str(source)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy is not valid YAML: {exc}") from exc
    return _parse_policy(data)


def builtin_policies() -> list[str]:
    """Names of the policies shipped with the package."""
    folder = resources.files("takapay_guard") / "policies"
    return sorted(p.name[: -len(".yaml")] for p in folder.iterdir() if p.name.endswith(".yaml"))


def load_builtin(name: str) -> Policy:
    """Load a shipped policy, for example ``"takapay-provenance-amount"``."""
    if name not in builtin_policies():
        raise PolicyError(f"no built-in policy {name!r}; choose from {builtin_policies()}")
    path = resources.files("takapay_guard") / "policies" / f"{name}.yaml"
    return _parse_policy(yaml.safe_load(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------- parsing helpers


def _mapping(value: Any, where: str, allowed: set[str], required: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{where}: expected a mapping, got {type(value).__name__}")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PolicyError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
    missing = sorted(required - set(value))
    if missing:
        raise PolicyError(f"{where}: missing key(s) {missing}")
    return value


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(f"{where}: expected true or false")
    return value


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{where}: expected a string or a list of strings")
    return tuple(value)


def _action(value: Any, where: str) -> str:
    if value not in ACTIONS:
        raise PolicyError(f"{where}: action must be one of {list(ACTIONS)}, got {value!r}")
    return value


def _regex(value: Any, where: str) -> re.Pattern[str]:
    try:
        return re.compile(_str(value, where))
    except re.error as exc:
        raise PolicyError(f"{where}: bad regular expression: {exc}") from exc


# ---------------------------------------------------------------- parsing


def _parse_policy(data: Any) -> Policy:
    top = _mapping(
        data,
        "policy",
        {"version", "name", "description", "default_action", "untrusted_tools", "codes", "sources", "consistency", "tools"},
        {"version", "name", "tools"},
    )
    if top["version"] != FORMAT_VERSION:
        raise PolicyError(f"policy: version must be {FORMAT_VERSION}, got {top['version']!r}")
    default_action = _action(top.get("default_action", "block"), "policy.default_action")

    codes = None
    if top.get("codes") is not None:
        c = _mapping(top["codes"], "codes", {"pattern", "context"}, {"pattern", "context"})
        codes = CodeRule(_regex(c["pattern"], "codes.pattern"), _regex(c["context"], "codes.context"))

    sources = []
    for i, s in enumerate(top.get("sources") or []):
        where = f"sources[{i}]"
        s = _mapping(
            s, where, {"tools", "payee_field", "payee_name_field", "payee_name_list", "amount_field", "id_fields"}, {"tools", "amount_field"}
        )
        rule = SourceRule(
            tools=_str_list(s["tools"], f"{where}.tools"),
            amount_field=_str(s["amount_field"], f"{where}.amount_field"),
            payee_field=_str(s.get("payee_field", ""), f"{where}.payee_field"),
            payee_name_field=_str(s.get("payee_name_field", ""), f"{where}.payee_name_field"),
            payee_name_list=_str(s.get("payee_name_list", ""), f"{where}.payee_name_list"),
            id_fields=_str_list(s.get("id_fields", ["id"]), f"{where}.id_fields"),
        )
        if not rule.payee_field and not rule.payee_name_field:
            raise PolicyError(f"{where}: give payee_field, payee_name_field or both")
        if bool(rule.payee_name_field) != bool(rule.payee_name_list):
            raise PolicyError(f"{where}: payee_name_field and payee_name_list go together")
        sources.append(rule)

    ignore_above = None
    if top.get("consistency") is not None:
        c = _mapping(top["consistency"], "consistency", {"ignore_above"})
        if "ignore_above" in c:
            if not isinstance(c["ignore_above"], (int, float)) or isinstance(c["ignore_above"], bool):
                raise PolicyError("consistency.ignore_above: expected a number")
            ignore_above = float(c["ignore_above"])

    tools_raw = top["tools"]
    if not isinstance(tools_raw, dict) or not tools_raw:
        raise PolicyError("tools: expected a non-empty mapping of tool name to rules")
    tools = {}
    for tool_name, t in tools_raw.items():
        where = f"tools.{tool_name}"
        t = _mapping(t, where, {"action", "arguments"}, {"arguments"})
        tool_action = _action(t.get("action", default_action), f"{where}.action")
        args_raw = t["arguments"]
        if not isinstance(args_raw, dict) or not args_raw:
            raise PolicyError(f"{where}.arguments: expected a non-empty mapping")
        args = tuple(
            _parse_argument(arg_name, a, f"{where}.arguments.{arg_name}", tool_action, set(args_raw), codes, sources)
            for arg_name, a in args_raw.items()
        )
        tools[str(tool_name)] = ToolRule(str(tool_name), args)

    return Policy(
        name=_str(top["name"], "policy.name"),
        description=_str(top.get("description", ""), "policy.description"),
        untrusted_tools=frozenset(_str_list(top.get("untrusted_tools") or [], "untrusted_tools")),
        codes=codes,
        sources=tuple(sources),
        consistency_ignore_above=ignore_above,
        tools=tools,
    )


def _parse_argument(
    name: str, a: Any, where: str, tool_action: str, siblings: set[str], codes: CodeRule | None, sources: list[SourceRule]
) -> ArgumentRule:
    a = _mapping(
        a,
        where,
        {
            "type",
            "trusted_from",
            "payee",
            "consistent",
            "no_untrusted_codes",
            "action",
            "message",
            "advice",
            "invalid_message",
            "consistent_message",
            "consistent_advice",
        },
        {"type"},
    )
    vtype = a["type"]
    if vtype not in VALUE_TYPES:
        raise PolicyError(f"{where}.type: must be one of {list(VALUE_TYPES)}, got {vtype!r}")
    trusted_from = _str_list(a.get("trusted_from", []), f"{where}.trusted_from")
    for src in trusted_from:
        if src not in (SOURCE_USER, SOURCE_PAYEE) and not (src.startswith(LIST_PREFIX) and len(src) > len(LIST_PREFIX)):
            raise PolicyError(f"{where}.trusted_from: unknown source {src!r} (use user_request, list:<name> or payee_sources)")
    rule = ArgumentRule(
        name=str(name),
        type=vtype,
        trusted_from=trusted_from,
        payee=_str(a.get("payee", ""), f"{where}.payee"),
        consistent=_bool(a.get("consistent", False), f"{where}.consistent"),
        no_untrusted_codes=_bool(a.get("no_untrusted_codes", False), f"{where}.no_untrusted_codes"),
        action=_action(a.get("action", tool_action), f"{where}.action"),
        message=_str(a.get("message", ""), f"{where}.message"),
        advice=_str(a.get("advice", ""), f"{where}.advice"),
        invalid_message=_str(a.get("invalid_message", ""), f"{where}.invalid_message"),
        consistent_message=_str(a.get("consistent_message", ""), f"{where}.consistent_message"),
        consistent_advice=_str(a.get("consistent_advice", ""), f"{where}.consistent_advice"),
    )
    if not rule.trusted_from and not rule.no_untrusted_codes:
        raise PolicyError(f"{where}: give trusted_from, no_untrusted_codes or both (otherwise nothing is checked)")
    if vtype == "text" and rule.trusted_from:
        raise PolicyError(f"{where}: a text argument supports only no_untrusted_codes")
    if rule.no_untrusted_codes and codes is None:
        raise PolicyError(f"{where}: no_untrusted_codes needs a top-level 'codes' section")
    needs_payee = SOURCE_PAYEE in trusted_from or rule.consistent
    if needs_payee:
        if vtype != "amount":
            raise PolicyError(f"{where}: payee_sources and consistent apply only to amount arguments")
        if not rule.payee:
            raise PolicyError(f"{where}: payee_sources and consistent need 'payee' (the argument naming the payee)")
        if rule.payee not in siblings:
            raise PolicyError(f"{where}.payee: {rule.payee!r} is not an argument of this tool")
        if not sources:
            raise PolicyError(f"{where}: payee_sources and consistent need a top-level 'sources' section")
    elif rule.payee:
        raise PolicyError(f"{where}.payee: only used with payee_sources or consistent")
    return rule
