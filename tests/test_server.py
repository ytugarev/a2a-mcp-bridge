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

from a2a_mcp_bridge.server import _extract_text


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


def test_extract_text_from_task_tuple_with_no_artifacts():
    task = Task(
        id="t2",
        context_id="c2",
        status=TaskStatus(state=TaskState.completed),
        artifacts=None,
    )
    payload = (task, None)
    assert _extract_text(payload) == str(payload)


def test_extract_text_falls_back_to_str_for_unknown_shape():
    assert _extract_text("plain string") == "plain string"
