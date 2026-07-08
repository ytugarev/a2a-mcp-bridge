# a2a-mcp-bridge

A minimal [MCP](https://modelcontextprotocol.io) server that bridges MCP clients
(Claude Desktop, etc.) to [A2A](https://a2a-protocol.org) agents — built directly on
[`a2a-sdk`](https://pypi.org/project/a2a-sdk/) `0.3.26+`, so it speaks the A2A
protocol version that current agent servers (e.g. Google ADK's `to_a2a`) actually
implement.

Stateless by design: no registry file, no local persistence, nothing written to
disk. Each call opens a connection, does the round trip, and returns — so it never
hits the file-permission traps that registry/cache-based bridges run into on
locked-down or sandboxed clients.

## Why this exists

The most visible community bridge, PyPI's `a2a-mcp-server`, does not depend on
`a2a-sdk` at all — it vendors a hand-copied client from the pre-1.0 draft A2A
protocol. Its hardcoded JSON-RPC methods are `tasks/send`, `tasks/sendSubscribe`,
`tasks/get`, `tasks/cancel`, `tasks/pushNotification/{get,set}`.

Current `a2a-sdk` (0.3.26+) servers use a different method set entirely:
`message/send`, `message/stream`, `tasks/get`, `tasks/cancel`,
`tasks/pushNotificationConfig/{get,set,list,delete}`, `tasks/resubscribe`,
`agent/getAuthenticatedExtendedCard`.

Point the old bridge at a modern A2A server and every call fails with
`-32601 Method not found` — the server has genuinely never heard of `tasks/send`.
This isn't a version skew you can fix by bumping a pin; the dialect changed.
`a2a-mcp-bridge` sidesteps the problem by using `a2a-sdk`'s own client
(`ClientFactory`, `send_message`), so it always speaks whatever protocol the SDK
you install implements.

## Install

**Option A — `uvx`, no clone, no install:**

```bash
uvx --from git+https://github.com/ytugarev/a2a-mcp-bridge a2a-mcp-bridge
```

**Option B — pip, from source:**

```bash
git clone https://github.com/ytugarev/a2a-mcp-bridge
cd a2a-mcp-bridge
pip install -e .
```

**Option C — single file, zero install:** `src/a2a_mcp_bridge/server.py` carries a
[PEP 723](https://peps.python.org/pep-0723/) inline metadata header, so you can
download that one file and run it directly with `uv` — `uv` resolves and caches
its dependencies on first launch, no `pip install` and no venv to manage:

```bash
uv run --script /absolute/path/to/server.py
```

## Configure

Two environment variables, both optional:

| Variable                | Default                 | Purpose                                                          |
|-------------------------|--------------------------|-------------------------------------------------------------------|
| `A2A_AGENT_URL`         | `http://localhost:8001` | Default A2A agent endpoint (also overridable per tool call)      |
| `A2A_TIMEOUT_SECONDS`   | `300`                    | HTTP client timeout — raise this for slow/long-running agents    |
| `A2A_HEARTBEAT_SECONDS` | `15`                     | Interval between MCP progress notifications while a task runs   |

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "a2a-bridge": {
      "command": "a2a-mcp-bridge",
      "env": {
        "A2A_AGENT_URL": "http://192.168.1.50:8001"
      }
    }
  }
}
```

Using the single-file `uv run --script` form instead (Option C above):

```json
{
  "mcpServers": {
    "a2a-bridge": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--script", "/absolute/path/to/server.py"],
      "env": {
        "A2A_AGENT_URL": "http://192.168.1.50:8001"
      }
    }
  }
}
```

**Both `command` and every path in `args` must be absolute.** MCP clients launch
server subprocesses with the working directory set to some system default (on
Windows, Claude Desktop uses `C:\WINDOWS\System32`) and often a stripped `PATH`,
so a bare `"uv"` or a relative `"server.py"` will silently fail to resolve —
this is the single most common setup error with this bridge (and MCP servers in
general), not a bug in the bridge itself. On Windows, also double check your
actual config path: Store-installed Claude Desktop keeps it under
`AppData\Local\Packages\<package-id>\LocalCache\Roaming\Claude\`, not the
`%APPDATA%\Claude` path most docs assume.

## Tools exposed

- **`get_agent_card(agent_url?)`** — fetches the target agent's name,
  description, and skills from its `/.well-known/agent-card.json` card.
- **`send_a2a_message(message, agent_url?, task_id?, context_id?)`** — sends a
  message to the agent and returns its final text response. Uses A2A streaming
  internally when the agent supports it (falling back to a blocking send
  otherwise), but still returns one clean answer per call.
- **`get_a2a_task(task_id, agent_url?)`** — fetches the current state and
  result of a previously started task. The escape hatch for calls that timed
  out after the task had already started: the task keeps running server-side,
  and the timeout error names the `task_id` to check on.

All tools accept an optional `agent_url` override, so a single bridge
instance can talk to multiple agents if needed.

### Multi-turn conversations

Every `send_a2a_message` response ends with an ids footer:

```
[a2a task_id=... context_id=...]
```

Passing those ids back as `task_id` / `context_id` on the next call continues
the same task — which is how you answer a `[task input-required]` follow-up
question from the agent. Omitting them starts a fresh task. The bridge itself
stays stateless: the conversation state lives in the A2A server and the ids
travel through the MCP client's context.

### Long-running tasks and timeouts

While a task runs, the bridge emits MCP progress notifications — one per A2A
status event, plus a heartbeat every `A2A_HEARTBEAT_SECONDS` while the agent
is silent — so MCP clients that reset their tool-call timeout on progress
(per the MCP spec) won't kill a slow call. For agents that outlive even that,
raise `A2A_TIMEOUT_SECONDS`, and note that a timed-out task is not lost: the
error message includes its `task_id` for `get_a2a_task`.

## Requirements

- Python 3.10+
- An A2A server speaking `a2a-sdk` 0.3.x semantics (e.g. Google ADK's
  `to_a2a`, or anything else built on the same SDK). The bridge pins
  `a2a-sdk>=0.3.26,<1.0.0`: the 1.x SDK line moved to protobuf-based types
  and is a separate migration.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
