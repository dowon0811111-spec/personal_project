"""다음 음악 처방: 에이전트 실행 → 추천 검증.

Claude의 최종 추천은 코드가 한 번 더 검증한다. 도구 결과에 없던 아티스트나 곡,
사용자가 이미 들은 아티스트, 중복 아티스트는 걸러 낸다. 프롬프트의 규칙만 믿지 않는다.
"""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from taste_critic.agent.loop import AgentResult, ToolCall, run_agent
from taste_critic.analysis.review_context import ReviewContext
from taste_critic.lastfm import LastFmClient
from taste_critic.prompts.recommend import (
    MAX_RECS,
    MIN_RECS,
    build_fix_message,
    build_recommend_message,
    build_recommend_system,
)
from taste_critic.prompts.review import DEFAULT_TONE
from taste_critic.review import ReviewError, parse_json_model
from taste_critic.tools.recommend_tools import RecommendationState, build_recommend_tools, normalize

RETRY_MAX_ITERATIONS = 5
RETRY_MAX_TOOL_CALLS = 8


class RecommendationError(ReviewError):
    """검증을 통과한 추천이 최소 개수에 못 미칠 때 발생한다."""


class RawRecommendation(BaseModel):
    artist: str = Field(min_length=1)
    track: str = Field(min_length=1)
    reason: str = ""
    based_on: str = ""


class RawRecommendations(BaseModel):
    recommendations: list[RawRecommendation]


class Recommendation(BaseModel):
    model_config = ConfigDict(frozen=True)

    artist: str
    track: str
    reason: str
    based_on: str
    url: str = ""


class RecommendationResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    recommendations: list[Recommendation]
    tool_calls: list[ToolCall]
    hit_limit: bool


_PLAYS_PATTERN = re.compile(r"^(?P<name>.+?)\s+(?P<count>\d+)\s*회$")


def verify_based_on(text: str, playcounts: dict[str, int]) -> str:
    """'이름 N회' 형태의 근거 중 실제 재생 수와 일치하는 것만 남긴다. 확인할 수 없는 조각은 버린다."""
    kept = []
    for part in re.split(r"[,，]", text):
        match = _PLAYS_PATTERN.match(part.strip())
        if match and playcounts.get(normalize(match["name"])) == int(match["count"]):
            kept.append(part.strip())
    return ", ".join(kept)


def validate_recommendations(
    raw: list[RawRecommendation],
    listened: set[str],
    state: RecommendationState,
    playcounts: dict[str, int] | None = None,
) -> tuple[list[Recommendation], list[str]]:
    """(통과한 추천, 제외 사유 목록)을 돌려준다. 이름은 도구가 돌려준 표기로 바로잡는다.

    playcounts(정규화한 이름 -> 재생 수)가 주어지면 근거 문구를 실제 값과 대조한다.
    """
    valid: list[Recommendation] = []
    problems: list[str] = []
    seen: set[str] = set()
    for item in raw:
        label = f"{item.artist} - {item.track}"
        key = normalize(item.artist)
        if key in listened:
            problems.append(f"{label}: 사용자가 이미 들은 아티스트")
        elif key not in state.candidates:
            problems.append(f"{label}: 도구 결과에 나온 적 없는 아티스트")
        elif key in seen:
            problems.append(f"{label}: 같은 아티스트가 중복됨")
        else:
            track = next((t for t in state.top_tracks.get(key, []) if normalize(t.name) == normalize(item.track)), None)
            if track is None:
                problems.append(f"{label}: get_artist_top_tracks 결과에 없는 곡")
            else:
                seen.add(key)
                based_on = item.based_on if playcounts is None else verify_based_on(item.based_on, playcounts)
                valid.append(
                    Recommendation(
                        artist=state.candidates[key], track=track.name, reason=item.reason, based_on=based_on, url=track.url
                    )
                )
    return valid, problems


def _parse(result: AgentResult) -> list[RawRecommendation]:
    return parse_json_model(result.text, RawRecommendations).recommendations


def recommend(
    context: ReviewContext,
    listened_names: set[str],
    lastfm: LastFmClient,
    client: Any,
    tone: str = DEFAULT_TONE,
) -> RecommendationResult:
    listened = {normalize(name) for name in listened_names}
    playcounts = {normalize(a.name): a.playcount for a in context.artists}
    state = RecommendationState()
    registry = build_recommend_tools(lastfm, listened, state)
    system = build_recommend_system(tone)
    messages: list[dict[str, Any]] = [{"role": "user", "content": build_recommend_message(context)}]

    result = run_agent(client, system=system, messages=messages, registry=registry)
    calls, hit_limit = list(result.tool_calls), result.hit_limit
    valid, problems = validate_recommendations(_parse(result), listened, state, playcounts)

    if len(valid) < MIN_RECS:
        # 한 번만 문제점을 알려 주고 다시 시도하게 한다.
        messages.append({"role": "user", "content": build_fix_message(problems, MIN_RECS - len(valid))})
        retry = run_agent(
            client,
            system=system,
            messages=messages,
            registry=registry,
            max_iterations=RETRY_MAX_ITERATIONS,
            max_tool_calls=RETRY_MAX_TOOL_CALLS,
        )
        calls += retry.tool_calls
        hit_limit = hit_limit or retry.hit_limit
        valid, problems = validate_recommendations(_parse(retry), listened, state, playcounts)

    if len(valid) < MIN_RECS:
        raise RecommendationError(f"조건에 맞는 추천을 {MIN_RECS}곡 이상 찾지 못했습니다. 잠시 후 다시 시도해 주세요.")
    return RecommendationResult(recommendations=valid[:MAX_RECS], tool_calls=calls, hit_limit=hit_limit)
