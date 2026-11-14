# 취향 평론가 (Taste Critic)

Last.fm 청취 기록과 MusicBrainz 메타데이터를 바탕으로, Claude가 음악 평론가처럼 사용자의 취향을 해석해 리뷰를 쓰고 다음에 들을 음악을 처방하는 웹 서비스.

이 문서는 Claude Code가 작업 시작 전에 읽는 프로젝트 기준 문서다. 기획 배경은 `PLAN.md`와 Notion 기획서 "[프로젝트 기획서] 취향 평론가"에 있다.

## 핵심 원칙

- Claude의 역할은 데이터 요약이 아니라 해석, 글쓰기, 그리고 "어떤 데이터를 더 조회할지"의 판단이다. 단순 대시보드가 되면 기획 의도에서 벗어난다.
- 모든 리뷰와 답변은 실제로 조회한 데이터에 근거해야 한다. 조회하지 않은 수치나 사실을 지어내지 않는다. 데이터가 부족하면 부족하다고 말한다.
- 숫자를 나열하기보다 읽을거리가 되는 글을 우선한다. 다만 근거가 되는 수치는 글 안에서 구체적으로 언급한다.
- 확신이 없는 해석은 단정하지 말고 "~로 보인다", "~일 수 있다" 식으로 쓴다.

## ⚠️ Claude API 연결 규칙 (최우선)

Claude API 연결에 관한 정보는 `ai_helper.py`에 있는 것만 사용한다.

- **`ai_helper.py`는 절대 수정하지 않는다.** 내용 변경, 포맷팅, import 정리, 이름 변경, 이동, 삭제 모두 금지. 쓰지 않는 import가 있어도 그대로 둔다.
- Claude 호출은 반드시 `ai_helper.py`를 import해서 한다.
  - 단순 호출: `from ai_helper import ask_ai` 후 `ask_ai(prompt)` 사용
  - tool use처럼 `ask_ai`로 안 되는 호출: `from ai_helper import client`로 같은 클라이언트를 가져와 `client.messages.create(...)`를 호출
- 새 `Anthropic(...)` 클라이언트를 만들지 않는다. `ai_helper.py`의 `client`만 쓴다.
- 모델은 `ai_helper.py`에 적힌 `"claude-haiku-5.5"`만 쓴다. 다른 모델 ID를 쓰거나 모델용 환경 변수를 새로 만들지 않는다.
- 연결 정보(`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`)는 `ai_helper.py`가 `.env`에서 읽는다. 다른 코드에서 이 값을 다시 읽거나 다른 엔드포인트를 지정하지 않는다.
- `max_tokens`는 `ask_ai`와 같은 1000을 기본으로 한다. 리뷰 본문 등이 잘리면 임의로 바꾸지 말고 사용자에게 먼저 물어본다.
- `ask_ai`는 user 메시지 하나만 받고 시스템 프롬프트 인자가 없다. 톤 지시 같은 시스템 규칙이 필요하면
  - `ask_ai`를 쓸 때는 프롬프트 문자열 앞부분에 규칙을 넣고,
  - `client.messages.create`를 쓸 때는 `system` 파라미터로 넘긴다.
- 이 모델은 thinking이 켜져 있으면 `max_tokens` 1000을 생각에 다 써서 본문이 빈 채로 끝난다. `ask_ai`는 thinking을 끌 수 없으므로, 리뷰처럼 긴 출력은 `taste_critic/claude.py`의 `ask_without_thinking`을 쓴다. 같은 `client`, 같은 모델, 같은 `max_tokens`에 `thinking={"type": "disabled"}`만 더한 래퍼다.
- Claude 호출은 서버 측 Python 코드에서만 한다. API 키를 화면에 출력하거나 로그에 남기지 않는다.

## 기술 스택

`ai_helper.py`가 Python, `anthropic` SDK, Streamlit, python-dotenv, requests를 쓰므로 이에 맞춘다.

- 언어: Python
- 웹 UI: Streamlit
- LLM: `ai_helper.py` 경유 (위 연결 규칙 참고)
- HTTP: requests (Last.fm, MusicBrainz 호출)
- 환경 변수: python-dotenv (`.env`)
- 데이터 검증: pydantic
- 테스트: pytest
- 캐시: 1차는 메모리 + 파일 캐시(`.cache/`). Streamlit 화면 쪽은 `st.cache_data`도 쓸 수 있다.

## 외부 API

### Last.fm (주 데이터 소스)

- 베이스 URL: `https://ws.audioscrobbler.com/2.0/`, `format=json`
- 인증: 읽기 전용 호출은 API 키만 사용. 사용자 비밀번호나 세션 키는 다루지 않는다.
- 사용 메서드
  - `user.getTopArtists`, `user.getTopTracks`, `user.getTopTags`: `period` 값은 `7day`, `1month`, `3month`, `6month`, `12month`, `overall`
  - `user.getRecentTracks`: 시간대/요일 패턴 분석용. 타임스탬프(`from`, `to`)와 페이지네이션 사용
  - `artist.getSimilar`, `artist.getTopTags`, `artist.getInfo`
  - `tag.getTopArtists`
- 호출은 초당 약 5회 이하로 제한하고, 429나 일시 오류는 지수 백오프로 재시도한다.
- 응답 필드는 문자열 숫자(`"playcount": "123"`)가 많다. 경계 계층에서 pydantic으로 파싱해 숫자로 변환한다.

### MusicBrainz (보강용)

- 아티스트의 국가와 활동 시작 연도 보강에만 사용한다. 이 연도는 곡의 발매 연도가 아니므로 UI와 프롬프트에서 "발매"라고 쓰지 않는다.
- 조회 결과(없음 포함)는 `.cache/musicbrainz.json`에 30일간 저장한다(`taste_critic/cache.py`).
- 반드시 식별 가능한 `User-Agent` 헤더를 보낸다(앱 이름, 버전, 연락처).
- 초당 1회 이하로 호출한다. 전역 큐로 직렬화하고, 같은 MBID는 반드시 캐시한다.
- Last.fm 응답의 `mbid`가 비어 있는 경우가 흔하다. 이름 검색으로 대체하되 오매칭 가능성을 결과에 표시한다.

## 환경 변수

`.env`에 둔다. 저장소에는 실제 값은 커밋하지 않는다.

```
# ai_helper.py가 읽는 값 (이름 변경 금지)
ANTHROPIC_BASE_URL=
ANTHROPIC_API_KEY=

# 데이터 소스
LASTFM_API_KEY=
MUSICBRAINZ_USER_AGENT=taste-critic/0.1 (contact@example.com)

# Claude 요청 제한 (세션당 시간당, 0이면 제한 없음, 기본 20)
AI_CALLS_PER_HOUR=20
```

## 기능과 구현 방침

기능마다 "어디까지 코드가 하고, 어디부터 Claude가 하는가"를 구분한다.

1. 취향 리뷰 리포트
   - 코드: 선택한 기간의 상위 아티스트, 곡, 태그를 수집해 하나의 컨텍스트 객체로 정리
   - Claude: 평론 본문, 한 줄 총평, 점수
   - 톤 옵션: 진지한 평론가, 독설가, 다정한 친구. 톤 지시는 `prompts/`에 별도 블록으로 분리해 쉽게 추가/수정할 수 있게 한다.
   - 출력은 구조화(JSON)로 받는다: `headline`, `body`, `score`, `evidence[]`. 응답 텍스트에서 JSON을 꺼내 pydantic으로 검증하고, 실패하면 오류로 처리한다. UI는 이 구조를 렌더링한다.
2. 취향 변화 타임라인
   - 코드: 기간별 상위 태그/아티스트 비교, MusicBrainz로 발매 연도와 국가 분포 계산
   - Claude: 변화에 대한 해석 문단만 담당
3. 청취 패턴 발견
   - 코드: `getRecentTracks`에서 시간대(0~23)와 요일별 집계. 타임존은 사용자 설정 기준(기본 Asia/Seoul)
   - Claude: 집계 결과 해석. 표본이 적으면 그 한계를 먼저 언급하도록 지시
4. 다음 음악 처방
   - Claude가 tool use로 `artist.getSimilar`, `tag.getTopArtists`를 직접 호출 (`ai_helper.client` 사용)
   - 코드가 이미 들은 아티스트 목록으로 결과를 걸러내는 것을 보장한다. 프롬프트에만 의존하지 않는다.
   - 출력: 3~5곡, 곡마다 추천 이유와 근거가 된 사용자 청취 데이터
5. 대화형 질문
   - 질문에 맞는 도구를 Claude가 고르고 필요하면 여러 번 호출한 뒤 근거와 함께 답한다.
   - "밝은 음악" 같은 정서 기준은 Last.fm에 직접 필드가 없다. 태그와 장르로 추정한 것임을 답변에 밝힌다.
6. 공유 카드
   - 리뷰 총평과 대표 아티스트를 이미지 카드로 만든다. 구현 방식은 해당 마일스톤에서 정한다.

## Tool use 설계

- tool use 호출은 `from ai_helper import client`로 가져온 클라이언트와 `ai_helper.py`의 모델 ID로만 한다.
- 도구 정의는 `taste_critic/tools/`에 한 파일 하나로 두고, 정의(JSON schema)와 실행 함수를 같이 둔다.
- 도구 목록(초안): `get_top_artists`, `get_top_tracks`, `get_top_tags`, `get_recent_tracks`, `get_similar_artists`, `get_tag_top_artists`, `get_artist_info`, `lookup_musicbrainz`
- 에이전트 루프에는 최대 반복 횟수(기본 8)와 총 도구 호출 수 상한을 둔다. 상한에 닿으면 지금까지의 근거로 답하게 한다.
- 도구 결과는 Claude에 넘기기 전에 필요한 필드만 남겨 줄인다(토큰 절약). 원본 전체를 넘기지 않는다.
- 도구 실행 오류는 예외로 터뜨리지 말고 `is_error: True` 결과로 돌려줘서 Claude가 다른 경로를 고르게 한다.
- 사용자 입력(질문, 사용자명)은 도구 인자로 쓰이기 전에 pydantic으로 검증한다.
- 다음 음악 처방(`taste_critic/recommend.py`)은 Claude의 최종 답을 코드가 다시 검증한다: 이미 들은 아티스트, 도구 결과에 없던 아티스트와 곡, 중복 아티스트를 걸러 내고, 근거 문구의 "이름 N회"는 실제 재생 수와 대조해 맞는 것만 남긴다. 3곡 미만이면 문제점을 알려 한 번 재시도한다.
- 대화형 질문(`taste_critic/qa.py`)의 도구는 사용자명을 인자로 받지 않고 빌드 시점에 고정한다. 답은 `uses_listening_data` 표시를 달고, 청취 사실을 말하면서 이번 질문에서 성공한 도구 호출이 없으면 한 번 다시 조회시키고, 그래도 없으면 답을 막는다(이전 대화의 기억만으로 수치를 말하는 것을 막기 위함). 이전 대화는 질문과 최종 답만 넘기고 최근 4턴까지만 쓴다.
- 차트(`taste_critic/analysis/charts.py`)는 `chart.*`(전 세계)와 `geo.*`(나라별)를 쓴다. 나라 이름은 `COUNTRIES`에 있는 ISO 표기만 받는다(`South Korea`는 거절되고 `Korea, Republic of`만 된다). 차트 응답에는 순위 필드가 없어 응답 순서를 순위로 쓰고, 수치의 집계 기간은 알 수 없으므로 기간을 붙이지 않는다. 나라별 차트에는 재생 수와 태그 차트가 없다.
- 차트 분석은 모델이 이름만 보고 "신인", "K-pop", "한국 그룹" 같은 분류를 덧붙이는 일이 잦다. `chart_insight.py`가 데이터에 없는 이런 표현을 잡아 한 번 다시 쓰게 하고, 그래도 남으면 오류로 처리한다(실제 10회 중 4회 재작성, 실패 0회).
- "이미 들은 아티스트"는 전체 기간 상위 500명 기준이다. 그 밖의 아티스트는 걸러지지 않을 수 있다.

## 프롬프트 관리

- 프롬프트는 코드에 문자열로 흩뿌리지 않고 `taste_critic/prompts/`에 모은다.
- 시스템 규칙에 반드시 포함할 내용: 조회한 데이터만 근거로 쓸 것, 수치는 정확히 인용할 것, 불확실하면 헤지 표현을 쓸 것, 사용자가 이미 들은 아티스트는 처방에서 제외할 것.
- 프롬프트를 바꿀 때는 `evals/`의 샘플 입력으로 결과를 눈으로 비교하고, 바뀐 이유를 커밋 메시지에 남긴다.

## 디렉터리 구조 (초안)

```
ai_helper.py            # Claude API 연결 (수정 절대 금지)
app.py                  # Streamlit 진입점
taste_critic/
  lastfm/               # Last.fm 클라이언트, pydantic 모델
  musicbrainz/          # MusicBrainz 클라이언트, 큐/캐시
  agent/                # ai_helper를 쓰는 에이전트 루프
  tools/                # tool 정의와 실행 함수
  prompts/              # 시스템/톤 프롬프트
  analysis/             # 시간대/요일 집계, 기간 비교 등 순수 함수
tests/                  # pytest
evals/                  # 프롬프트 비교용 샘플 입력과 결과
.cache/                 # 로컬 캐시(커밋 금지)
```

## 코드 규칙

- 함수에는 타입 힌트를 단다. 외부 응답은 항상 pydantic으로 파싱한다.
- 외부 API 호출은 반드시 `taste_critic/lastfm`, `taste_critic/musicbrainz`를 거친다. Streamlit 화면 코드에서 직접 requests를 호출하지 않는다.
- Claude 호출은 반드시 `ai_helper.py`를 거친다(위 연결 규칙 참고).
- 집계, 비교 같은 계산은 순수 함수로 `taste_critic/analysis`에 두고 단위 테스트를 작성한다. LLM에게 계산을 시키지 않는다.
- 비밀 값이나 사용자 청취 기록 원본을 로그에 남기지 않는다.
- 사용자 청취 기록은 개인 정보다. 서버에 저장하더라도 최소한으로, 캐시 만료를 둔다.
- 에러 메시지와 UI 문구는 한국어, 코드 주석과 커밋 메시지는 한국어 또는 영어 중 한 가지로 통일한다(기본: 한국어).
- 문서와 UI 문구에서 em dash는 쓰지 않는다.

## 자주 쓰는 명령어

```
py -m pip install -r requirements.txt   # 의존성 설치
py -m streamlit run app.py              # 개발 서버
py -m pytest                            # 테스트
py -m taste_critic.lastfm <사용자명>     # 실제 Last.fm API 연결 확인
```

이 PC에서는 Python Scripts 폴더가 PATH에 없으므로 `py -m` 형식으로 실행한다.

작업을 끝내기 전에 `py -m pytest`를 통과시킨다.

## 구현 순서 (마일스톤)

1. 프로젝트 초기화, 환경 변수, Last.fm 클라이언트와 pydantic 모델, 단위 테스트
2. 취향 리뷰 리포트: 데이터 수집 → `ask_ai` 호출 → 구조화 출력 → 화면. 이 단계가 끝나면 서비스의 핵심은 동작해야 한다.
3. 청취 패턴 분석(순수 함수 + 해석)
4. MusicBrainz 보강과 취향 변화 타임라인
5. tool use 에이전트 루프(`ai_helper.client` 사용)와 다음 음악 처방
6. 대화형 질문
7. 톤 옵션 확장, 공유 카드, 캐시 정리, 배포

한 번에 여러 마일스톤을 구현하지 말고, 마일스톤 하나가 끝날 때마다 동작을 확인하고 멈춘다.

## 아직 결정되지 않은 것

작업 중 아래 항목을 만나면 임의로 정하지 말고 사용자에게 물어본다.

- 사용자 식별 방식: Last.fm 사용자명 입력만 받을지, 로그인 기능을 둘지
- 실제 배포 여부와 캐시 저장소(현재는 Streamlit Community Cloud를 가정해 `README.md`와 `packages.txt`만 준비했고, 배포는 하지 않았다)
- 응답이 `max_tokens` 1000에 걸려 잘릴 때의 대응(지금은 오류로 처리하고, 본문을 300자 안팎으로 쓰게 해 피한다)
- 공개 서비스로 크게 열 경우의 서버 측 사용량 제한(현재는 세션당 시간당 `AI_CALLS_PER_HOUR`회의 가벼운 제한뿐이다)

결정된 것(임의로 바꾸지 않는다): 리뷰 점수는 "취향의 폭, 깊이, 일관성을 종합한 10점 만점"이다(`prompts/review.py`의 `SCORE_CRITERIA`).
