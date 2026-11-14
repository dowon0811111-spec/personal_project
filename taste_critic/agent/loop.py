"""tool use 에이전트 루프.

ai_helper.client로 Claude를 부르고, Claude가 요청한 도구를 실행해 결과를 돌려주는 일을 반복한다.
반복 횟수와 도구 호출 수에 상한이 있고, 상한에 닿으면 도구 없이 지금까지의 근거로 답하게 한다.
모델과 max_tokens는 taste_critic.claude와 같다(ai_helper.ask_ai와 동일).
"""

from dataclasses import dataclass, field
from typing import Any

from taste_critic.claude import MAX_TOKENS, MODEL
from taste_critic.review import ReviewError
from taste_critic.tools.registry import ToolRegistry

MAX_ITERATIONS = 8
MAX_TOOL_CALLS = 12
LIMIT_MESSAGE = "도구 호출 한도에 도달했습니다. 지금까지 조회한 결과만으로 답하세요."


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict[str, Any]
    is_error: bool


@dataclass
class AgentResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    hit_limit: bool = False


def _assistant_content(response: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in response.content:
        if block.type == "text":
            blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return blocks


def run_agent(
    client: Any,
    *,
    system: str,
    messages: list[dict[str, Any]],
    registry: ToolRegistry,
    max_iterations: int = MAX_ITERATIONS,
    max_tool_calls: int = MAX_TOOL_CALLS,
) -> AgentResult:
    """messages에 대화를 이어 붙이며 실행한다. 마지막 assistant 답변도 messages에 추가된다."""
    calls: list[ToolCall] = []
    hit_limit = False
    for iteration in range(max_iterations):
        exhausted = iteration == max_iterations - 1 or len(calls) >= max_tool_calls
        hit_limit = hit_limit or exhausted
        request: dict[str, Any] = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "thinking": {"type": "disabled"},
            "system": system,
            "tools": registry.definitions(),
            "messages": messages,
        }
        if exhausted:
            request["tool_choice"] = {"type": "none"}
        response = client.messages.create(**request)
        if response.stop_reason == "max_tokens":
            raise ReviewError("Claude 응답이 길이 제한(max_tokens)에 걸려 잘렸습니다.")

        messages.append({"role": "assistant", "content": _assistant_content(response)})
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            text = "".join(block.text for block in response.content if block.type == "text")
            if not text.strip():
                raise ReviewError("Claude가 빈 응답을 돌려줬습니다.")
            return AgentResult(text, calls, hit_limit)
        if exhausted:
            raise ReviewError("Claude가 도구 호출 한도 이후에도 도구를 요청했습니다.")

        results = []
        for block in tool_uses:
            if len(calls) >= max_tool_calls:
                content, is_error = LIMIT_MESSAGE, True
            else:
                outcome = registry.execute(block.name, block.input)
                content, is_error = outcome.content, outcome.is_error
                calls.append(ToolCall(block.name, dict(block.input), is_error))
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    raise ReviewError("에이전트 루프가 끝나지 않았습니다.")  # 마지막 반복은 항상 도구 없이 답하므로 도달하지 않는다.
