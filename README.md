# 취향 평론가 (Taste Critic)

Last.fm 청취 기록과 MusicBrainz 메타데이터를 바탕으로, Claude가 음악 평론가처럼 취향을 해석하고 다음에 들을 음악을 처방하는 Streamlit 앱입니다. 기획은 [PLAN.md](PLAN.md), 개발 규칙은 [CLAUDE.md](CLAUDE.md)에 있습니다.

## 기능

| 탭 | 내용 |
| --- | --- |
| 취향 리뷰 | 기간별 상위 아티스트, 곡, 태그를 읽고 총평, 점수(10점 만점), 본문을 작성. 공유 카드(PNG) 저장 |
| 청취 패턴 | 시간대, 요일별 청취 집계와 해석 |
| 취향 변화 | 두 기간의 태그, 아티스트, 활동 시작 시기, 출신 국가 비교 |
| 다음 음악 | Claude가 도구로 후보를 찾아 이미 들은 아티스트를 뺀 3~5곡 처방 |
| 차트 | Last.fm 전 세계 또는 나라별 인기 차트를 조회하고 분석. 나라 차트는 전 세계 차트와 비교하고, 사용자명을 넣으면 내가 이미 듣는 아티스트와 겹침도 계산 |
| 대화 | 내 청취 기록과 차트에 대한 질문에 도구로 조회해 답변 |

## 실행

```
py -m pip install -r requirements.txt
copy .env.example .env      # 값을 채운다
py -m streamlit run app.py
py -m pytest                # 테스트
```

`.env`의 `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`는 [ai_helper.py](ai_helper.py)가 읽습니다. 이 파일은 수정하지 않습니다.

Last.fm API 키는 https://www.last.fm/api/account/create 에서 발급합니다.

## 배포 (Streamlit Community Cloud 기준)

1. 저장소를 GitHub에 올립니다. `.env`와 `.cache/`는 `.gitignore`에 있어 올라가지 않습니다.
2. https://share.streamlit.io 에서 `app.py`를 진입점으로 앱을 만듭니다.
3. 앱 설정의 Secrets에 `.env.example`의 항목을 TOML 형식으로 넣습니다. 최상위 문자열 값은 환경 변수로도 읽혀 코드를 바꿀 필요가 없습니다.
   ```
   ANTHROPIC_BASE_URL = "..."
   ANTHROPIC_API_KEY = "..."
   LASTFM_API_KEY = "..."
   MUSICBRAINZ_USER_AGENT = "taste-critic/0.1 (본인 연락처)"
   AI_CALLS_PER_HOUR = "20"
   ```
4. [packages.txt](packages.txt)가 서버에 한글 글꼴(fonts-nanum)을 설치해 공유 카드의 글자가 깨지지 않게 합니다.

## 알아 둘 점

- **사용량 제한**: Claude 요청은 세션당 시간당 `AI_CALLS_PER_HOUR`회로 제한됩니다. 새로고침으로 우회할 수 있는 가벼운 제한이므로, 공개 서비스로 크게 열 때는 IP나 계정 기준의 서버 측 제한이 필요합니다.
- **캐시**: MusicBrainz 조회는 `.cache/musicbrainz.json`에 30일 저장됩니다. 클라우드 서버는 재시작 시 이 파일이 사라질 수 있습니다.
- **Last.fm 이용 조건**: 기본은 비상업 이용이며 출처 표기가 필요합니다. 앱 하단과 공유 카드에 표기했습니다.
- **개인 정보**: 청취 기록은 개인 정보입니다. 이 앱은 공개 프로필의 기록만 조회하고 서버에 따로 저장하지 않습니다(MusicBrainz 결과 캐시 제외).
