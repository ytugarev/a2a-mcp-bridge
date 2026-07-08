import pytest

from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)

from a2a_mcp_bridge import server
from a2a_mcp_bridge.server import _extract_text, _ids_footer


def _text_part(text: str) -> Part:
    return Part(root=TextPart(text=text))


def test_extract_text_from_message():
    msg = Message(
        message_id="m1",
        role=Role.agent,
        parts=[_text_part("hello"), _text_part("world")],
    )
    assert _extract_text(msg) == "hello\nworld"


def test_extract_text_from_task_tuple_joins_all_artifact_parts():
    task = Task(
        id="t1",
        context_id="c1",
        status=TaskStatus(state=TaskState.completed),
        artifacts=[
            Artifact(artifact_id="a1", parts=[_text_part("draft")]),
            Artifact(artifact_id="a2", parts=[_text_part("critique")]),
        ],
    )
    assert _extract_text((task, None)) == "draft\ncritique"


def test_extract_text_completed_task_with_no_artifacts_falls_back_to_task_str():
    task = Task(
        id="t2",
        context_id="c2",
        status=TaskStatus(state=TaskState.completed),
        artifacts=None,
    )
    # No artifacts, no status message, completed: nothing meaningful to return.
    # Falls back to str(task) -- NOT str of the whole (task, update) tuple.
    assert _extract_text((task, None)) == str(task)


def test_extract_text_failed_task_surfaces_status_message_and_state():
    task = Task(
        id="t3",
        context_id="c3",
        status=TaskStatus(
            state=TaskState.failed,
            message=Message(
                message_id="s3",
                role=Role.agent,
                parts=[_text_part("upstream model timed out")],
            ),
        ),
        artifacts=None,
    )
    result = _extract_text((task, None))
    assert result == "[task failed] upstream model timed out"


def test_extract_text_input_required_surfaces_followup_question():
    task = Task(
        id="t4",
        context_id="c4",
        status=TaskStatus(
            state=TaskState.input_required,
            message=Message(
                message_id="s4",
                role=Role.agent,
                parts=[_text_part("which environment: staging or prod?")],
            ),
        ),
        artifacts=None,
    )
    result = _extract_text((task, None))
    assert result == "[task input-required] which environment: staging or prod?"


def test_extract_text_completed_task_prefers_artifacts_over_status_message():
    task = Task(
        id="t5",
        context_id="c5",
        status=TaskStatus(
            state=TaskState.completed,
            message=Message(
                message_id="s5",
                role=Role.agent,
                parts=[_text_part("ignore me")],
            ),
        ),
        artifacts=[Artifact(artifact_id="a5", parts=[_text_part("the answer")])],
    )
    assert _extract_text((task, None)) == "the answer"


def test_extract_text_falls_back_to_str_for_unknown_shape():
    assert _extract_text("plain string") == "plain string"


def test_ids_footer_with_both_ids():
    assert _ids_footer("t1", "c1") == "\n\n[a2a task_id=t1 context_id=c1]"


def test_ids_footer_omits_missing_ids():
    assert _ids_footer("t1", None) == "\n\n[a2a task_id=t1]"
    assert _ids_footer(None, None) == ""


class _FakeA2AClient:
    """Stands in for the a2a-sdk Client: replays canned send_message results
    (raising any Exception in the list mid-stream) and records what was sent."""

    def __init__(self, results=(), task=None):
        self._results = list(results)
        self._task = task
        self.sent_messages = []
        self.queried_params = []

    async def send_message(self, msg):
        self.sent_messages.append(msg)
        for result in self._results:
            if isinstance(result, Exception):
                raise result
            yield result

    async def get_task(self, params):
        self.queried_params.append(params)
        return self._task


def _fake_factory(client):
    class _FakeFactory:
        @classmethod
        async def connect(cls, agent_url, client_config=None):
            return client

    return _FakeFactory


def _completed_task(task_id="t1", context_id="c1", answer="the answer") -> Task:
    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.completed),
        artifacts=[Artifact(artifact_id="a1", parts=[_text_part(answer)])],
    )


@pytest.mark.asyncio
async def test_send_message_appends_ids_footer(monkeypatch):
    client = _FakeA2AClient(results=[(_completed_task(), None)])
    monkeypatch.setattr(server, "ClientFactory", _fake_factory(client))

    result = await server.send_a2a_message("hi")
    assert result == "the answer\n\n[a2a task_id=t1 context_id=c1]"


@pytest.mark.asyncio
async def test_send_message_passes_continuation_ids(monkeypatch):
    client = _FakeA2AClient(results=[(_completed_task(), None)])
    monkeypatch.setattr(server, "ClientFactory", _fake_factory(client))

    await server.send_a2a_message("staging", task_id="t1", context_id="c1")
    sent = client.sent_messages[0]
    assert sent.task_id == "t1"
    assert sent.context_id == "c1"


@pytest.mark.asyncio
async def test_send_message_fresh_call_has_no_ids(monkeypatch):
    client = _FakeA2AClient(results=[(_completed_task(), None)])
    monkeypatch.setattr(server, "ClientFactory", _fake_factory(client))

    await server.send_a2a_message("hi")
    sent = client.sent_messages[0]
    assert sent.task_id is None
    assert sent.context_id is None


@pytest.mark.asyncio
async def test_send_message_error_after_task_started_names_task_id(monkeypatch):
    working = Task(
        id="t9",
        context_id="c9",
        status=TaskStatus(state=TaskState.working),
    )
    client = _FakeA2AClient(results=[(working, None), TimeoutError("read timed out")])
    monkeypatch.setattr(server, "ClientFactory", _fake_factory(client))

    result = await server.send_a2a_message("hi")
    assert result.startswith("A2A Error: read timed out")
    assert "get_a2a_task" in result
    assert "task_id=t9" in result


@pytest.mark.asyncio
async def test_send_message_error_before_task_started_has_no_rescue_hint(monkeypatch):
    client = _FakeA2AClient(results=[ConnectionError("refused")])
    monkeypatch.setattr(server, "ClientFactory", _fake_factory(client))

    result = await server.send_a2a_message("hi")
    assert result == "A2A Error: refused"


@pytest.mark.asyncio
async def test_get_a2a_task_returns_text_and_footer(monkeypatch):
    client = _FakeA2AClient(task=_completed_task(task_id="t7", context_id="c7"))
    monkeypatch.setattr(server, "ClientFactory", _fake_factory(client))

    result = await server.get_a2a_task("t7")
    assert result == "the answer\n\n[a2a task_id=t7 context_id=c7]"
    assert client.queried_params[0].id == "t7"
