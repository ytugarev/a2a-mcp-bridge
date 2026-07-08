# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.0.0", "a2a-sdk>=0.3.26,<1.0.0", "httpx>=0.27"]
# ///
"""A2A <-> MCP bridge.

Exposes an A2A agent (e.g. a Google ADK `to_a2a` server) as MCP tools, so any
MCP client (Claude Desktop, etc.) can call it. Built directly on `a2a-sdk`
(0.3.26+), which speaks the current A2A JSON-RPC dialect: `message/send`,
`message/stream`, `tasks/get`, ... .

Older community bridges (e.g. `a2a-mcp-server` on PyPI) predate `a2a-sdk` and
hardcode a draft-era method set (`tasks/send`, `tasks/sendSubscribe`, ...).
Against a modern A2A server those calls fail with JSON-RPC -32601 ("Method
not found") because the server has never heard of the old method names —
there's no version to bump, the dialect itself changed. This bridge avoids
that by using `a2a-sdk`'s own client, so it tracks the protocol the SDK
implements rather than a hand-copied snapshot of it.

Configure via environment variables (see README) or MCP client args.
"""

import asyncio
import os
import time

import httpx
from mcp.server.fastmcp import Context, FastMCP

from a2a.client.client_factory import ClientFactory, ClientConfig
from a2a.client.helpers import create_text_message_object
from a2a.types import Message, Task, TaskQueryParams

DEFAULT_AGENT_URL = os.environ.get("A2A_AGENT_URL", "http://localhost:8001")
TIMEOUT_SECONDS = float(os.environ.get("A2A_TIMEOUT_SECONDS", "300"))
HEARTBEAT_SECONDS = float(os.environ.get("A2A_HEARTBEAT_SECONDS", "15"))

mcp = FastMCP("a2a-mcp-bridge")


def _texts_from_parts(parts) -> list[str]:
    """Join the text of every TextPart in a parts list, skipping File/DataParts.

    Some agent pipelines tag every text chunk with metadata (e.g.
    `adk_thought: True`) instead of marking one part as the "final answer",
    so this deliberately does not filter on part metadata -- it just takes
    every TextPart it finds.
    """
    return [p.root.text for p in (parts or []) if getattr(p.root, "text", None)]


def _extract_text(result: Task | Message | tuple) -> str:
    """Pull the meaningful text out of a send_message result.

    For a Task, artifacts hold the answer on success -- but on `failed` /
    `input-required` (and other states that produce no artifact) the relevant
    text lives in `task.status.message` (the failure reason or the agent's
    follow-up question). We check artifacts first, then fall back to the
    status message, so those states surface as legible text instead of a raw
    object dump. Non-`completed` terminal states are prefixed so the caller
    can tell an answer from an error.
    """
    if isinstance(result, tuple):
        task, _update = result
    elif isinstance(result, Message):
        return "\n".join(_texts_from_parts(result.parts)) or str(result)
    else:
        return str(result)

    texts = [t for a in (task.artifacts or []) for t in _texts_from_parts(a.parts)]

    status = getattr(task, "status", None)
    state = getattr(status, "state", None)
    if not texts and status is not None and status.message is not None:
        texts = _texts_from_parts(status.message.parts)

    body = "\n".join(texts)
    state_value = getattr(state, "value", state)
    if state_value and state_value != "completed":
        label = f"[task {state_value}]"
        return f"{label} {body}".rstrip() if body else label
    return body or str(task)


def _ids_footer(task_id: str | None, context_id: str | None) -> str:
    """Trailing line carrying the ids needed to continue or check on a task.

    Appended to every response that involved a task, so the MCP client can
    pass the ids back (send_a2a_message task_id/context_id, get_a2a_task)
    without the bridge holding any state between calls.
    """
    fields = " ".join(
        f"{key}={value}"
        for key, value in (("task_id", task_id), ("context_id", context_id))
        if value
    )
    return f"\n\n[a2a {fields}]" if fields else ""


@mcp.tool()
async def get_agent_card(agent_url: str = DEFAULT_AGENT_URL) -> str:
    """Fetch capabilities (name, description, skills) of an A2A agent."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as httpx_client:
            config = ClientConfig(streaming=False, httpx_client=httpx_client)
            client = await ClientFactory.connect(agent_url, client_config=config)
            card = await client.get_card()
            skills = ", ".join(s.name for s in card.skills) if card.skills else "None"
            return f"Agent Name: {card.name}\nDescription: {card.description}\nSkills: {skills}"
    except Exception as e:
        return f"Connection failed: {e}"


@mcp.tool()
async def send_a2a_message(
    message: str,
    agent_url: str = DEFAULT_AGENT_URL,
    task_id: str | None = None,
    context_id: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Send a message to an A2A agent and return its final text response.

    The response ends with an `[a2a task_id=... context_id=...]` line. To
    continue the same conversation -- in particular to answer a
    `[task input-required]` question -- pass those ids back via the
    `task_id` / `context_id` arguments on the next call; omitting them
    starts a fresh task.

    Long-running agent pipelines are expected: progress notifications are
    emitted while the task runs, so tune A2A_TIMEOUT_SECONDS rather than
    treating a slow response as a hang. If the call does time out after the
    task started, the error names the task_id -- the task keeps running
    server-side and get_a2a_task can retrieve its result.
    """
    start = time.monotonic()
    last_state = "sending message"

    async def report(note: str) -> None:
        # Progress is best-effort: a failed notification must not kill the
        # in-flight A2A call. FastMCP no-ops when the client sent no
        # progressToken.
        if ctx is None:
            return
        try:
            await ctx.report_progress(time.monotonic() - start, None, note)
        except Exception:
            pass

    async def heartbeat() -> None:
        # Keeps MCP client tool-call timeouts alive while the agent is
        # silent between A2A events.
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await report(f"{last_state} ({int(time.monotonic() - start)}s elapsed)")

    seen_task: Task | None = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as httpx_client:
            # streaming=True uses message/stream when the agent's card
            # advertises streaming (each SSE event also resets the httpx read
            # timeout); the SDK falls back to blocking message/send otherwise.
            config = ClientConfig(streaming=True, httpx_client=httpx_client)
            client = await ClientFactory.connect(agent_url, client_config=config)
            msg = create_text_message_object(content=message)
            msg.task_id = task_id
            msg.context_id = context_id

            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                last_result = None
                async for result in client.send_message(msg):
                    last_result = result
                    if isinstance(result, tuple):
                        seen_task = result[0]
                        state = getattr(seen_task.status, "state", None)
                        state_value = getattr(state, "value", state)
                        if state_value:
                            last_state = f"task {state_value}"
                        await report(last_state)
            finally:
                heartbeat_task.cancel()

            if last_result is None:
                return "A2A Error: agent returned no result"
            if isinstance(last_result, tuple):
                footer = _ids_footer(last_result[0].id, last_result[0].context_id)
            elif isinstance(last_result, Message):
                footer = _ids_footer(last_result.task_id, last_result.context_id)
            else:
                footer = ""
            return _extract_text(last_result) + footer
    except Exception as e:
        rescue = ""
        if seen_task is not None:
            rescue = (
                f" -- the task may still be running server-side; check it with"
                f" get_a2a_task (task_id={seen_task.id})"
            )
        return f"A2A Error: {e}{rescue}"


@mcp.tool()
async def get_a2a_task(task_id: str, agent_url: str = DEFAULT_AGENT_URL) -> str:
    """Fetch the current state and result of a previously started A2A task.

    Use this when send_a2a_message timed out or errored after the task had
    already started: the task keeps running server-side, and this retrieves
    its status and any output by the task_id from the error message or the
    `[a2a ...]` response footer.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as httpx_client:
            config = ClientConfig(streaming=False, httpx_client=httpx_client)
            client = await ClientFactory.connect(agent_url, client_config=config)
            task = await client.get_task(TaskQueryParams(id=task_id))
            return _extract_text((task, None)) + _ids_footer(task.id, task.context_id)
    except Exception as e:
        return f"A2A Error: {e}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
