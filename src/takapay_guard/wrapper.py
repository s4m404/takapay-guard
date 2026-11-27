"""A guard for an OpenAI-compatible tool-calling loop.

Wrap your tool functions once, start the guard for each user task, and pass
every tool call the model makes through :meth:`GuardedTools.call`. The
call is checked before it runs; a blocked call is not run and the model
gets the reason in its place. Every result that runs is recorded before
the model sees it.

    tools = GuardedTools(Guard(load_builtin("takapay-provenance-amount")),
                         {"read_sms": read_sms, "send_money": send_money})
    tools.start(user_request, lists={"contacts": contacts, "billers": billers})
    messages = [{"role": "user", "content": user_request}]
    reply = run_openai_loop(client, "my-model", messages, tool_schemas, tools)

Nothing here imports the ``openai`` package: any client with
``client.chat.completions.create(model=..., messages=..., tools=...)`` works,
and so do plain dicts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .engine import Decision, Guard

# The block message the TakaPay benchmark showed the agent.
BLOCK_TEMPLATE = "BLOCKED by security policy: {reason}. {advice}"
DEFAULT_ADVICE = "Ask the user for explicit approval before retrying."


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from an SDK object or a plain dict."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass
class CallRecord:
    """One tool call as the wrapper handled it."""

    tool: str
    args: dict[str, Any]
    decision: Decision
    ran: bool
    content: str


class GuardedTools:
    """Your tool functions, behind a :class:`Guard`.

    ``ask_user``: called with a decision whose action is ``"ask"``; return
    True to run the call anyway (the user approved). Without it, an "ask"
    call is not run and the model is told to ask the user.
    """

    def __init__(
        self,
        guard: Guard,
        functions: Mapping[str, Callable[..., Any]],
        ask_user: Callable[[Decision], bool] | None = None,
        block_template: str = BLOCK_TEMPLATE,
        default_advice: str = DEFAULT_ADVICE,
    ):
        self.guard = guard
        self.functions = dict(functions)
        self.ask_user = ask_user
        self.block_template = block_template
        self.default_advice = default_advice
        self.history: list[CallRecord] = []

    def start(self, user_request: str, **kwargs: Any) -> None:
        """Begin a new user task (see :meth:`Guard.start`)."""
        self.guard.start(user_request, **kwargs)
        self.history = []

    def blocked_message(self, decision: Decision) -> str:
        return self.block_template.format(reason=decision.reason, advice=decision.advice or self.default_advice)

    def call(self, tool: str, args: Any) -> str:
        """Check one tool call, run it if allowed, record its result, and
        return the text for the model. ``args`` may be a JSON string."""
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                return self._note(tool, {}, Decision("allow", tool=tool), False, f"Error: the arguments of {tool} are not valid JSON.")
        if not isinstance(args, dict):
            return self._note(tool, {}, Decision("allow", tool=tool), False, f"Error: the arguments of {tool} must be a JSON object.")
        decision = self.guard.check(tool, args)
        if decision.action == "ask" and self.ask_user is not None and self.ask_user(decision):
            decision = Decision("allow", f"approved by the user: {decision.reason}", tool=tool, argument=decision.argument,
                                value=decision.value, rule=decision.rule, seen_in=decision.seen_in)
        if not decision.allowed:
            return self._note(tool, args, decision, False, self.blocked_message(decision))
        fn = self.functions.get(tool)
        if fn is None:
            return self._note(tool, args, decision, False, f"Error: unknown tool {tool}.")
        try:
            result = fn(**args)
        except Exception as exc:  # report the error to the model, as tool runners do
            result = f"Error executing tool {tool}: {exc}"
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        self.guard.record_tool_result(tool, args, content)
        return self._note(tool, args, decision, True, content)

    def _note(self, tool: str, args: dict[str, Any], decision: Decision, ran: bool, content: str) -> str:
        self.history.append(CallRecord(tool, args, decision, ran, content))
        return content

    def handle(self, message: Any) -> list[dict[str, Any]]:
        """Run every tool call of one assistant message; return the
        ``{"role": "tool", ...}`` messages to append, in order."""
        out = []
        for tc in _get(message, "tool_calls") or []:
            fn = _get(tc, "function")
            content = self.call(_get(fn, "name", ""), _get(fn, "arguments", "") or "")
            out.append({"role": "tool", "tool_call_id": _get(tc, "id", ""), "content": content})
        return out


def _as_dict(message: Any) -> dict[str, Any]:
    """An assistant message as a plain dict, ready to send back."""
    if isinstance(message, Mapping):
        return dict(message)
    calls = []
    for tc in _get(message, "tool_calls") or []:
        fn = _get(tc, "function")
        calls.append({"id": _get(tc, "id", ""), "type": "function",
                      "function": {"name": _get(fn, "name", ""), "arguments": _get(fn, "arguments", "") or ""}})
    out: dict[str, Any] = {"role": "assistant", "content": _get(message, "content")}
    if calls:
        out["tool_calls"] = calls
    return out


def run_openai_loop(
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    tools: GuardedTools,
    max_steps: int = 10,
    **create_kwargs: Any,
) -> str:
    """A minimal agent loop: call the model, run its tool calls through the
    guard, repeat until it answers without tools. ``messages`` is extended in
    place; the final answer text is returned ("" if ``max_steps`` ran out).
    Call ``tools.start(...)`` first."""
    for _ in range(max_steps):
        response = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas, **create_kwargs)
        message = _get(_get(response, "choices")[0], "message")
        messages.append(_as_dict(message))
        if not _get(message, "tool_calls"):
            return _get(message, "content") or ""
        messages.extend(tools.handle(message))
    return ""
