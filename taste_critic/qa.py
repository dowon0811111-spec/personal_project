"""대화형 질문: 질문 → 에이전트(도구 조회) → 근거가 있는 답.

이전 대화는 질문과 최종 답(JSON 문자열)만 이어 붙인다. 도구 호출 기록은 넘기지 않아 토큰을 아낀다.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from taste_critic.agent.loop import ToolCall, run_agent
from taste_critic.lastfm import LastFmClient
from taste_critic.musicbrainz import MusicBrainzClient
from taste_critic.prompts.qa import build_qa_system
from taste_critic.prompts.review import BODY_MAX_CHARS, DEFAULT_TONE, build_shorten_prompt
from taste_critic.review import Insight, ReviewError, parse_model
from taste_critic.tools.qa_tools import build_qa_tools

MAX_QUESTION_CHARS = 300
MAX_HISTORY_TURNS = 4


def validate_question(raw: str) -> str:
    text = raw.strip()
    if not 2 <= len(text) <= MAX_QUESTION_CHARS:
        raise ValueError(f"질문은 2~{MAX_QUESTION_CHARS}자로 입력해 주세요.")
    return text


class QAAnswer(Insight):
    # 기본값을 True로 둬서, 표시를 빠뜨린 답도 조회 근거가 있는지 검사받게 한다.
    uses_listening_data: bool = True


NO_LOOKUP_MESSAGE = (
    "이번 질문에서 도구를 한 번도 성공적으로 호출하지 않았는데, 답에 청취에 관한 사실이 들어 있다. "
    "청취에 관한 사실과 수치는 이번 질문에서 도구로 조회한 결과만 근거로 쓸 수 있다. "
    "필요한 도구를 호출한 뒤 같은 JSON 형식으로 다시 답하라."
)


def _needs_lookup(answer: QAAnswer, calls: list[ToolCall]) -> bool:
    return answer.uses_listening_data and not any(not call.is_error for call in calls)


class QATurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str
    answer_text: str  # 모델이 쓴 JSON 문자열 그대로


class QAResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    question: str
    answer: QAAnswer
    answer_text: str
    tool_calls: list[ToolCall]
    hit_limit: bool

    def as_turn(self) -> QATurn:
        return QATurn(question=self.question, answer_text=self.answer_text)


def _history_messages(history: list[QATurn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer_text})
    return messages


def answer_question(
    client: Any,
    lastfm: LastFmClient,
    musicbrainz: MusicBrainzClient,
    user: str,
    question: str,
    history: list[QATurn] | None = None,
    tone: str = DEFAULT_TONE,
) -> QAResult:
    question = validate_question(question)
    registry = build_qa_tools(lastfm, musicbrainz, user)
    system = build_qa_system(tone)
    messages = _history_messages(history or []) + [{"role": "user", "content": question}]

    result = run_agent(client, system=system, messages=messages, registry=registry)
    answer = parse_model(result.text, QAAnswer)
    text, calls, hit_limit = result.text, list(result.tool_calls), result.hit_limit

    if _needs_lookup(answer, calls):
        # 조회 없이 청취 사실을 말했다. 한 번만 조회하도록 다시 시킨다.
        messages.append({"role": "user", "content": NO_LOOKUP_MESSAGE})
        retry = run_agent(client, system=system, messages=messages, registry=registry, max_iterations=5, max_tool_calls=6)
        answer = parse_model(retry.text, QAAnswer)
        text, calls, hit_limit = retry.text, calls + retry.tool_calls, hit_limit or retry.hit_limit
        if _needs_lookup(answer, calls):
            raise ReviewError("조회한 데이터 없이 답하려 해서 답변을 막았습니다. 질문을 조금 바꿔 다시 시도해 주세요.")

    if len(answer.body) > BODY_MAX_CHARS:
        # 글자 수 제한을 넘으면 직전 답을 보여 주고 한 번만 줄이게 한다.
        messages.append({"role": "user", "content": build_shorten_prompt("", answer.body).strip()})
        retry = run_agent(client, system=system, messages=messages, registry=registry, max_iterations=2, max_tool_calls=0)
        answer = parse_model(retry.text, QAAnswer)
        text = retry.text
        if len(answer.body) > BODY_MAX_CHARS:
            raise ReviewError(f"본문이 {BODY_MAX_CHARS}자 제한을 넘었습니다 ({len(answer.body)}자).")
    return QAResult(question=question, answer=answer, answer_text=text, tool_calls=calls, hit_limit=hit_limit)
