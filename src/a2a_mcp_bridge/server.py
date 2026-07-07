# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.0.0", "a2a-sdk>=0.3.26", "httpx>=0.27"]
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

import os

import httpx
from mcp.server.fastmcp import FastMCP

from a2a.client.client_factory import ClientFactory, ClientConfig
from a2a.client.helpers import create_text_message_object
from a2a.types import Message, Task

DEFAULT_AGENT_URL = os.environ.get("A2A_AGENT_URL", "http://localhost:8001")
TIMEOUT_SECONDS = float(os.environ.get("A2A_TIMEOUT_SECONDS", "300"))

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
async def send_a2a_message(message: str, agent_url: str = DEFAULT_AGENT_URL) -> str:
    """Send a message to an A2A agent and return its final text response.

    Non-streaming: waits for the task to complete and returns one clean
    answer. Long-running agent pipelines are expected -- tune
    A2A_TIMEOUT_SECONDS rather than treating a slow response as a hang.
    """
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as httpx_client:
            config = ClientConfig(streaming=False, httpx_client=httpx_client)
            client = await ClientFactory.connect(agent_url, client_config=config)
            msg = create_text_message_object(content=message)

            last_result = None
            async for result in client.send_message(msg):
                last_result = result
            if last_result is None:
                return "A2A Error: agent returned no result"
            return _extract_text(last_result)
    except Exception as e:
        return f"A2A Error: {e}"


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
