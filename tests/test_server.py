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
