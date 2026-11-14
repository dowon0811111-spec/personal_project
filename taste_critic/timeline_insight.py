"""취향 변화 해석 생성: 비교 데이터 → Claude → 구조화 출력."""

from collections.abc import Callable

from taste_critic.analysis.timeline import TimelineContext
from taste_critic.prompts.review import DEFAULT_TONE
from taste_critic.prompts.timeline import build_timeline_prompt
from taste_critic.review import Insight, ask_within_limit


def generate_timeline_insight(
    context: TimelineContext,
    ask: Callable[[str], str],
    tone: str = DEFAULT_TONE,
) -> Insight:
    return ask_within_limit(build_timeline_prompt(context, tone), ask, Insight)
