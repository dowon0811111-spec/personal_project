"""Claude 호출 결과를 구조화된 글로 바꾼다: 프롬프트 → Claude → JSON 검증 → 글자 수 제한.

Claude 호출 함수는 인자로 받는다. 실제로는 claude.ask_without_thinking을 넘긴다.
"""

import json
import re
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, Field, ValidationError

from taste_critic.analysis.review_context import ReviewContext
from taste_critic.prompts.review import (
    BODY_MAX_CHARS,
    DEFAULT_TONE,
    SCORE_MAX,
    SCORE_MIN,
    build_review_prompt,
    build_shorten_prompt,
)

_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class ReviewError(Exception):
    """Claude 응답을 쓸 수 없을 때 발생한다."""


class Insight(BaseModel):
    """제목, 본문, 근거로 이루어진 해석 글."""

    headline: str = Field(min_length=1)
    body: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)


class Review(Insight):
    score: int = Field(ge=SCORE_MIN, le=SCORE_MAX)


M = TypeVar("M", bound=Insight)


T = TypeVar("T", bound=BaseModel)


def parse_json_model(text: str, model: type[T]) -> T:
    """응답 텍스트에서 JSON 객체를 꺼내 model로 검증한다."""
    cleaned = _FENCE_PATTERN.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ReviewError("Claude 응답에서 JSON을 찾지 못했습니다. 응답이 중간에 잘렸을 수 있습니다.")
    try:
        return model.model_validate(json.loads(cleaned[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ReviewError("Claude 응답이 기대한 형식과 맞지 않습니다. 응답이 중간에 잘렸을 수 있습니다.") from exc


def parse_model(text: str, model: type[M]) -> M:
    return parse_json_model(text, model)


def parse_review(text: str) -> Review:
    return parse_model(text, Review)


def parse_insight(text: str) -> Insight:
    return parse_model(text, Insight)


def ask_within_limit(prompt: str, ask: Callable[[str], str], model: type[M]) -> M:
    """본문이 BODY_MAX_CHARS를 넘으면 한 번 줄여 쓰게 하고, 그래도 넘으면 실패시킨다."""
    result = parse_model(ask(prompt), model)
    if len(result.body) > BODY_MAX_CHARS:
        result = parse_model(ask(build_shorten_prompt(prompt, result.body)), model)
        if len(result.body) > BODY_MAX_CHARS:
            raise ReviewError(f"본문이 {BODY_MAX_CHARS}자 제한을 넘었습니다 ({len(result.body)}자).")
    return result


def generate_review(
    context: ReviewContext,
    ask: Callable[[str], str],
    tone: str = DEFAULT_TONE,
) -> Review:
    return ask_within_limit(build_review_prompt(context, tone), ask, Review)
