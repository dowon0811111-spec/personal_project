import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from taste_critic.agent.loop import LIMIT_MESSAGE, run_agent
from taste_critic.claude import MAX_TOKENS, MODEL
from taste_critic.lastfm import LastFmError
from taste_critic.review import ReviewError
from taste_critic.tools.registry import Tool, ToolRefusal, ToolRegistry


class Args(BaseModel):
    artist: str


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool_use(id, name, **input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def reply(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


class FakeClient:
    """준비된 응답을 순서대로 돌려주고 요청 내용을 기록한다."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": json.loads(json.dumps(kwargs["messages"]))})
        return self._responses.pop(0)


def registry(run=lambda args: ["ok"]):
    return ToolRegistry([Tool("lookup", "조회", Args, run)])


def test_tool_loop_executes_tool_and_returns_final_text():
    client = FakeClient(
        reply("tool_use", text("조회할게요"), tool_use("t1", "lookup", artist="IU")),
        reply("end_turn", text("끝")),
    )
    messages = [{"role": "user", "content": "시작"}]

    result = run_agent(client, system="sys", messages=messages, registry=registry(lambda args: [args.artist]))

    assert result.text == "끝"
    assert [(c.name, c.input, c.is_error) for c in result.tool_calls] == [("lookup", {"artist": "IU"}, False)]
    first = client.requests[0]
    assert (first["model"], first["max_tokens"], first["thinking"], first["system"]) == (
        MODEL, MAX_TOKENS, {"type": "disabled"}, "sys",
    )
    assert "tool_choice" not in first
    second_messages = client.requests[1]["messages"]
    assert second_messages[1]["content"][1] == {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"artist": "IU"}}
    assert second_messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": '["IU"]', "is_error": False}
    ]
    assert messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": "끝"}]}


def test_tool_errors_are_returned_as_is_error_results_not_raised():
    def run(args):
        if args.artist == "refuse":
            raise ToolRefusal("거절됨")
        if args.artist == "missing":
            raise LastFmError("찾을 수 없습니다")
        raise RuntimeError("secret-internal-detail")

    client = FakeClient(
        reply(
            "tool_use",
            tool_use("a", "lookup", artist="refuse"),
            tool_use("b", "lookup", artist="missing"),
            tool_use("c", "lookup", artist="boom"),
            tool_use("d", "nope", artist="x"),
            tool_use("e", "lookup", wrong=1),
        ),
        reply("end_turn", text("done")),
    )

    result = run_agent(client, system="s", messages=[{"role": "user", "content": "go"}], registry=registry(run))

    results = client.requests[1]["messages"][2]["content"]
    assert all(r["is_error"] for r in results)
    assert [r["content"] for r in results][:2] == ["거절됨", "찾을 수 없습니다"]
    assert not any("secret-internal-detail" in r["content"] for r in results)
    assert all(c.is_error for c in result.tool_calls)


def test_tool_call_limit_stops_tools_and_forces_final_answer():
    client = FakeClient(
        reply("tool_use", tool_use("a", "lookup", artist="1"), tool_use("b", "lookup", artist="2")),
        reply("end_turn", text("한도 후 답")),
    )

    result = run_agent(client, system="s", messages=[{"role": "user", "content": "go"}], registry=registry(), max_tool_calls=1)

    assert len(result.tool_calls) == 1
    assert result.hit_limit
    results = client.requests[1]["messages"][2]["content"]
    assert results[1]["content"] == LIMIT_MESSAGE and results[1]["is_error"]
    assert client.requests[1]["tool_choice"] == {"type": "none"}


def test_iteration_limit_forces_last_turn_without_tools():
    client = FakeClient(
        reply("tool_use", tool_use("a", "lookup", artist="1")),
        reply("end_turn", text("마지막")),
    )

    result = run_agent(client, system="s", messages=[{"role": "user", "content": "go"}], registry=registry(), max_iterations=2)

    assert result.text == "마지막" and result.hit_limit
    assert "tool_choice" not in client.requests[0]
    assert client.requests[1]["tool_choice"] == {"type": "none"}


def test_tool_request_after_limit_raises():
    client = FakeClient(reply("tool_use", tool_use("a", "lookup", artist="1")))

    with pytest.raises(ReviewError):
        run_agent(client, system="s", messages=[{"role": "user", "content": "go"}], registry=registry(), max_iterations=1)


@pytest.mark.parametrize("response", [reply("max_tokens", text("잘림")), reply("end_turn")])
def test_truncated_or_empty_response_raises(response):
    with pytest.raises(ReviewError):
        run_agent(FakeClient(response), system="s", messages=[{"role": "user", "content": "go"}], registry=registry())


def test_definitions_are_clean_json_schema():
    definition = registry().definitions()[0]

    assert definition["name"] == "lookup"
    assert definition["input_schema"]["required"] == ["artist"]
    assert "title" not in definition["input_schema"]
    assert "title" not in definition["input_schema"]["properties"]["artist"]
