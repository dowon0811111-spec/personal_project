"""차트 분석 생성: 차트 데이터 → Claude → 구조화 출력.

모델은 아티스트 이름만 보고 "신인", "K-pop", "한국 그룹" 같은 분류를 덧붙이곤 한다.
프롬프트 규칙만으로는 막히지 않아, 데이터에 없는 이런 표현이 나오면 코드가 잡아 한 번 다시 쓰게 한다.
"""

import re
from collections.abc import Callable

from taste_critic.analysis.charts import ChartReport
from taste_critic.prompts.charts import build_chart_prompt
from taste_critic.prompts.review import DEFAULT_TONE
from taste_critic.review import Insight, ReviewError, ask_within_limit

_COUNTRY_WORDS = "한국|일본|미국|영국|독일|프랑스|캐나다|호주|브라질|멕시코|인도|이탈리아|스페인|스웨덴"
UNSUPPORTED_PATTERNS = (
    re.compile(r"신인"),
    re.compile(r"걸\s?그룹|보이\s?그룹|아이돌"),
    re.compile(r"[KJkj][\s-]?(?:pop|팝)|케이팝|제이팝", re.IGNORECASE),
    re.compile(r"[0-9]{2,4}년대\s?생|[MZ]\s?세대|[1-9]0\s?세대|세대"),
    re.compile(rf"(?:{_COUNTRY_WORDS})\s?(?:출신\s?)?(?:그룹|아티스트|가수|밴드|뮤지션)"),
)


def find_unsupported_terms(text: str, source_text: str) -> list[str]:
    """text에 있고 source_text(차트 데이터)에는 없는 분류 표현을 찾는다."""
    source = source_text.casefold()
    found: list[str] = []
    for pattern in UNSUPPORTED_PATTERNS:
        for match in pattern.finditer(text):
            term = match.group(0)
            if term.casefold() not in source and term not in found:
                found.append(term)
    return found


def _fix_prompt(prompt: str, previous: Insight, terms: list[str]) -> str:
    return (
        f"{prompt}\n\n[재요청] 직전 답변에 [차트 데이터]에 없는 배경 지식 표현이 있었다: {', '.join(terms)}\n"
        f"직전 headline: {previous.headline}\n직전 body: {previous.body}\n\n"
        "위 표현과 그 뜻이 같은 표현을 쓰지 말고, 데이터에 있는 순위와 수치, 겹침과 차이만으로 같은 JSON 형식으로 다시 쓴다."
    )


def generate_chart_insight(
    report: ChartReport,
    ask: Callable[[str], str],
    tone: str = DEFAULT_TONE,
) -> Insight:
    prompt = build_chart_prompt(report, tone)
    source = report.to_prompt_text()
    insight = ask_within_limit(prompt, ask, Insight)
    terms = find_unsupported_terms(f"{insight.headline}\n{insight.body}", source)
    if terms:
        insight = ask_within_limit(_fix_prompt(prompt, insight, terms), ask, Insight)
        terms = find_unsupported_terms(f"{insight.headline}\n{insight.body}", source)
        if terms:
            raise ReviewError(f"분석에 차트 데이터에 없는 표현이 남았습니다 ({', '.join(terms)}). 잠시 후 다시 시도해 주세요.")
    return insight
