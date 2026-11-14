"""청취 패턴 해석 생성: 집계 결과 → Claude → 구조화 출력."""

from collections.abc import Callable

from taste_critic.analysis.patterns import ListeningPattern
from taste_critic.prompts.pattern import build_pattern_prompt
from taste_critic.prompts.review import DEFAULT_TONE
from taste_critic.review import Insight, ask_within_limit


def generate_pattern_insight(
    pattern: ListeningPattern,
    ask: Callable[[str], str],
    tone: str = DEFAULT_TONE,
) -> Insight:
    return ask_within_limit(build_pattern_prompt(pattern, tone), ask, Insight)
