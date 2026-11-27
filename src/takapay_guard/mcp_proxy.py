"""An MCP proxy: sits between any MCP client and any MCP server and checks
every ``tools/call`` request with a :class:`Guard`.

    takapay-guard-mcp --policy takapay-provenance-amount \\
        --request-file ~/current_task.txt \\
        --list contacts=tool:list_contacts --list billers=tool:list_billers \\
        -- python -m mfs_env.server --demo

The client starts the proxy instead of the server. The proxy starts the real
server, lists its tools unchanged, and for each tool call:

- ``allow``: forwards the call, records the result, returns it unchanged;
- ``block``: does not forward it and returns an error result with the reason;
- ``ask``: asks the user through MCP elicitation (a form in the client, not
  the model) and forwards the call only if the user approves. If the client
  cannot ask the user, the call is blocked.

MCP does not carry the user's own request, so the proxy cannot see it. Give
it with ``--request`` or ``--request-file`` (the file is read again on every
call; when its text changes, a new task starts). Without it, only the
trusted lists count, which is stricter, never looser.

Needs the ``mcp`` package: ``pip install "takapay-guard[mcp]"``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from .engine import Decision, Guard
from .policy import builtin_policies, load_builtin, load_policy
from .wrapper import BLOCK_TEMPLATE, DEFAULT_ADVICE

APPROVAL_KEY = "takapay_guard_approval"
APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {"approve": {"type": "boolean", "title": "Allow this call?"}},
    "required": ["approve"],
}


def result_text(result: CallToolResult) -> str:
    """The text of a tool result, as the guard records it."""
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    if texts:
        return "\n".join(texts)
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    return ""


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


class GuardProxy:
    """The guard in front of one connected downstream MCP client session.

    ``request``: the user's request, or a callable returning it (read on
    every call; when the text changes, a new task starts).
    ``lists``: trusted lists, each a value (list or mapping) or ``"tool:NAME"``
    to read it from a downstream tool's JSON result at the start of a task.
    ``background``: tools to call at the start of a task whose results feed
    the consistency check (for example ``read_sms``).
    """

    def __init__(
        self,
        guard: Guard,
        downstream: Client,
        request: str | Any = "",
        lists: dict[str, Any] | None = None,
        background: list[str] | None = None,
        block_template: str = BLOCK_TEMPLATE,
        default_advice: str = DEFAULT_ADVICE,
    ):
        self.guard = guard
        self.downstream = downstream
        self._request = request
        self.lists = dict(lists or {})
        self.background = list(background or [])
        self.block_template = block_template
        self.default_advice = default_advice
        self._task: str | None = None
        self._secret = secrets.token_bytes(32)
        self.events: list[dict[str, Any]] = []

    # ------------------------------------------------------------ tasks

    def current_request(self) -> str:
        return str(self._request() if callable(self._request) else self._request)

    async def _call_json(self, tool: str) -> Any:
        text = result_text(await self.downstream.call_tool(tool, {}))
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def ensure_task(self) -> None:
        """Start a new task if the user's request changed (or none started yet)."""
        request = self.current_request()
        if request == self._task:
            return
        lists = {}
        for name, source in self.lists.items():
            if isinstance(source, str) and source.startswith("tool:"):
                value = await self._call_json(source[len("tool:") :])
                lists[name] = value if isinstance(value, (list, dict)) else []
            else:
                lists[name] = source
        background = {tool: await self._call_json(tool) for tool in self.background}
        self.guard.start(request, lists=lists, background=background)
        self._task = request

    # ------------------------------------------------------------ handlers

    async def list_tools(self, ctx: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
        return await self.downstream.list_tools(cursor=params.cursor if params else None)

    async def call_tool(self, ctx: Any, params: CallToolRequestParams) -> CallToolResult | InputRequiredResult:
        await self.ensure_task()
        tool, args = params.name, dict(params.arguments or {})
        decision = self.guard.check(tool, args)
        if decision.action == "ask":
            approved = await self._ask_user(ctx, params, tool, args, decision)
            if isinstance(approved, InputRequiredResult):
                return approved
            self.events.append({"tool": tool, "asked": True, "approved": approved})
            if approved:
                decision = Decision("allow", f"approved by the user: {decision.reason}", tool=tool,
                                    argument=decision.argument, value=decision.value, rule=decision.rule)
        if not decision.allowed:
            self.events.append({"tool": tool, "args": args, "action": decision.action, "reason": decision.reason})
            return _error(self.block_template.format(reason=decision.reason, advice=decision.advice or self.default_advice))
        result = await self.downstream.call_tool(tool, args)
        self.guard.record_tool_result(tool, args, result_text(result))
        self.events.append({"tool": tool, "args": args, "action": "allow"})
        return result

    def _state(self, tool: str, args: dict[str, Any]) -> str:
        """A tag that binds an approval to this exact call."""
        body = json.dumps([self._task, tool, args], sort_keys=True, ensure_ascii=False).encode()
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def _question(self, tool: str, decision: Decision) -> str:
        where = f" It was found in: {', '.join(decision.seen_in)}." if decision.seen_in else ""
        return (f"takapay-guard: the agent wants to call {tool} with {decision.argument} = {decision.value}. "
                f"{decision.reason[:1].upper()}{decision.reason[1:]}.{where} Allow this call?")

    async def _ask_user(self, ctx: Any, params: CallToolRequestParams, tool: str, args: dict[str, Any],
                        decision: Decision) -> bool | InputRequiredResult:
        """Ask the user through elicitation. Returns True or False, or (on the
        2026-07-28 protocol) an InputRequiredResult the client answers and retries."""
        form = ElicitRequestFormParams(message=self._question(tool, decision), requested_schema=APPROVAL_SCHEMA)
        try:
            caps = ctx.session.client_capabilities
        except Exception:
            caps = None
        if caps is None or caps.elicitation is None:
            return False  # the client cannot ask the user: do not run the call
        if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
            answer = (params.input_responses or {}).get(APPROVAL_KEY)
            if answer is not None:
                # Accept the answer only for the very call it was asked about.
                if not hmac.compare_digest(params.request_state or "", self._state(tool, args)):
                    return False
                return _approved(answer)
            return InputRequiredResult(input_requests={APPROVAL_KEY: ElicitRequest(params=form)},
                                       request_state=self._state(tool, args))
        try:
            answer = await ctx.session.elicit_form(form.message, form.requested_schema, related_request_id=ctx.request_id)
        except Exception:  # no way to reach the user: do not run the call
            return False
        return _approved(answer)

    def server(self) -> Server:
        info = self.downstream.server_info
        return Server(
            f"takapay-guard ({info.name})" if info else "takapay-guard",
            instructions=self.downstream.instructions,
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
        )


def _approved(answer: Any) -> bool:
    if isinstance(answer, dict):
        answer = ElicitResult.model_validate(answer)
    return isinstance(answer, ElicitResult) and answer.action == "accept" and (answer.content or {}).get("approve") is True


# ---------------------------------------------------------------- command line


def _parse_list(spec: str) -> tuple[str, Any]:
    name, sep, source = spec.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"--list wants NAME=tool:TOOL or NAME=file:PATH, got {spec!r}")
    if source.startswith("file:"):
        return name, json.loads(Path(source[len("file:") :]).expanduser().read_text(encoding="utf-8"))
    if source.startswith("tool:"):
        return name, source
    raise argparse.ArgumentTypeError(f"--list source must start with tool: or file:, got {source!r}")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("usage: takapay-guard-mcp [options] -- COMMAND [ARGS...]  (see --help)", file=sys.stderr)
        if "-h" not in argv and "--help" not in argv:
            raise SystemExit(2)
    split = argv.index("--") if "--" in argv else len(argv)
    ap = argparse.ArgumentParser(prog="takapay-guard-mcp", description="Check MCP tool calls with a takapay-guard policy.")
    ap.add_argument("--policy", required=True, help=f"a YAML file or a built-in policy: {', '.join(builtin_policies())}")
    req = ap.add_mutually_exclusive_group()
    req.add_argument("--request", default="", help="the user's request (trusted)")
    req.add_argument("--request-file", type=Path, help="a file with the user's request, read on every call")
    ap.add_argument("--list", dest="lists", action="append", default=[], type=_parse_list,
                    help="a trusted list: NAME=tool:TOOL (read from the server) or NAME=file:PATH (JSON)")
    ap.add_argument("--background", action="append", default=[], help="a tool whose result feeds the consistency check")
    ap.add_argument("--cwd", help="working folder for the server command")
    opts = ap.parse_args(argv[:split])
    command = argv[split + 1 :]
    if not command:
        ap.error("give the server command after --")

    policy = load_builtin(opts.policy) if opts.policy in builtin_policies() else load_policy(Path(opts.policy))
    if opts.request_file:
        path = opts.request_file.expanduser()
        request: Any = lambda: path.read_text(encoding="utf-8").strip() if path.exists() else ""  # noqa: E731
    else:
        request = opts.request

    async def run() -> None:
        params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ), cwd=opts.cwd)
        async with Client(params) as down:
            proxy = GuardProxy(Guard(policy), down, request, dict(opts.lists), opts.background)
            server = proxy.server()
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
