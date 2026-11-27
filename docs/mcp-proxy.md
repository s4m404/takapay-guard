# MCP proxy

`takapay-guard-mcp` sits between an MCP client and an MCP server. The
client starts the proxy instead of the server; the proxy starts the real
server and checks every `tools/call` with a takapay-guard policy.

```bash
pip install -e ".[mcp]"
takapay-guard-mcp --policy takapay-provenance-amount \
    --request "রাফিকে ৫০০ টাকা পাঠাও" \
    --list contacts=tool:list_contacts --list billers=tool:list_billers \
    -- python -m mfs_env.server --demo
```

Everything after `--` is the real server's command.

## What it does with each call

| decision | what the proxy does |
|---|---|
| allow | forwards the call, records the result (text content) for later checks, returns it unchanged |
| block | does not forward it; returns an error result: `BLOCKED by security policy: <reason>. <advice>` |
| ask | asks the user through MCP elicitation: the client shows a yes/no form to the user, not to the model. Forwards the call only if the user says yes. If the client does not support elicitation, the call is blocked |

Tool lists are passed through unchanged. Both protocol versions work: the
2026-07-28 version (the proxy returns an "input required" result that the
client answers and retries; the answer is bound to that exact call, so it
cannot approve a different one) and the older handshake version (the proxy
sends an elicitation request).

## Options

| option | meaning |
|---|---|
| `--policy` | a built-in policy name or a YAML file |
| `--request TEXT` | the user's request (trusted) |
| `--request-file PATH` | a file holding the user's request; read again on every call. When its text changes, a new task starts (state from the old task is dropped) |
| `--list NAME=tool:TOOL` | a trusted list, read from the server's own tool at the start of each task (for example the wallet's saved contacts) |
| `--list NAME=file:PATH` | a trusted list from a JSON file (a list, or a mapping of name to value) |
| `--background TOOL` | call this tool at the start of each task and give its result to the consistency check |
| `--cwd PATH` | working folder for the server command |

## The user's request

MCP carries tool calls, not the user's chat, so the proxy cannot see what
the user asked. Give it with `--request` or `--request-file` (a host app or
a small script can write the current task to the file). Without it, only
the trusted lists count: a value the user typed is then blocked too. That
is stricter, never looser.

## Using it from an MCP client

Clients that start servers from a config file take the proxy the same way.
For example, in a `mcpServers` config:

```json
{
  "mcpServers": {
    "takapay-wallet": {
      "command": "takapay-guard-mcp",
      "args": ["--policy", "takapay-provenance-amount", "--request-file", "/home/me/task.txt",
               "--list", "contacts=tool:list_contacts", "--list", "billers=tool:list_billers",
               "--", "python", "-m", "mfs_env.server", "--demo"]
    }
  }
}
```

This has been run with the Python MCP SDK client (`scripts/mcp_demo.py`,
`tests/test_mcp_proxy.py`), not yet with a desktop client.

## Limits

- Only tools are proxied; resources and prompts of the server are not
  passed through yet.
- Stdio only (the proxy is started by the client; the server is started by
  the proxy).
- Only the text content of results is recorded (or the structured content
  when there is no text); images are ignored.
