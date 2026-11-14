from types import SimpleNamespace

import pytest

from taste_critic.claude import MAX_TOKENS, MODEL, ask_without_thinking
from taste_critic.review import ReviewError


class FakeClient:
    def __init__(self, stop_reason, blocks):
        self.requests = []
        self._response = SimpleNamespace(stop_reason=stop_reason, content=blocks)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._response


def text(value):
    return SimpleNamespace(type="text", text=value)


def test_sends_expected_request_and_joins_text_blocks():
    client = FakeClient("end_turn", [SimpleNamespace(type="thinking"), text("가"), text("나")])

    assert ask_without_thinking("질문", client) == "가나"
    request = client.requests[0]
    assert request["model"] == MODEL
    assert request["max_tokens"] == MAX_TOKENS
    assert request["thinking"] == {"type": "disabled"}
    assert request["messages"] == [{"role": "user", "content": "질문"}]


def test_truncated_response_raises():
    client = FakeClient("max_tokens", [text('{"headline": "잘림')])

    with pytest.raises(ReviewError):
        ask_without_thinking("질문", client)
