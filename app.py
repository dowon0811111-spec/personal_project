"""취향 평론가 Streamlit 앱.

실행: py -m streamlit run app.py
"""

import pandas as pd
import streamlit as st

from taste_critic.analysis.patterns import (
    DEFAULT_TIMEZONE,
    TIME_BUCKETS,
    WEEKDAY_LABELS,
    ListeningPattern,
    collect_pattern,
)
from taste_critic.analysis.review_context import (
    PERIOD_LABELS,
    InsufficientDataError,
    ReviewContext,
    collect_review_context,
)
from taste_critic.analysis.timeline import TimelineContext, collect_timeline, country_label
from taste_critic.cache import DiskCache
from taste_critic.claude import ask_without_thinking
from taste_critic.config import ConfigError, load_settings
from taste_critic.musicbrainz import MusicBrainzClient, MusicBrainzError
from taste_critic.agent.loop import ToolCall
from taste_critic.analysis.charts import ChartReport, collect_chart_report
from taste_critic.chart_insight import generate_chart_insight
from taste_critic.share_card import CardFontError, render_share_card
from taste_critic.usage import UsageLimiter, UsageLimitError
from taste_critic.qa import QAResult, answer_question
from taste_critic.recommend import RecommendationResult, recommend
from taste_critic.timeline_insight import generate_timeline_insight
from taste_critic.lastfm import COUNTRIES, PERIODS, LastFmClient, LastFmError, validate_period, validate_username
from taste_critic.pattern_insight import generate_pattern_insight
from taste_critic.prompts.review import DEFAULT_TONE, SCORE_MAX, TONES
from taste_critic.review import Insight, Review, ReviewError, generate_review

st.set_page_config(page_title="취향 평론가", page_icon="🎧")

LISTENED_LIMIT = 500
PATTERN_DAYS = {7: "최근 7일", 30: "최근 30일", 90: "최근 90일"}
TIMEZONES = (DEFAULT_TIMEZONE, "Asia/Tokyo", "UTC", "Europe/London", "America/New_York", "America/Los_Angeles")


def use_ai_quota() -> None:
    """Claude를 부르기 직전에 호출한다. 세션당 시간당 허용 횟수를 넘으면 UsageLimitError."""
    limiter = st.session_state.setdefault("ai_limiter", UsageLimiter(load_settings().ai_calls_per_hour))
    limiter.consume()


@st.cache_data(ttl=3600, show_spinner=False)
def load_context(user: str, period: str) -> ReviewContext:
    # 같은 사용자와 기간은 1시간 동안 Last.fm을 다시 부르지 않는다.
    client = LastFmClient(load_settings().lastfm_api_key)
    return collect_review_context(client, user, validate_period(period))


@st.cache_data(ttl=1800, show_spinner=False)
def load_pattern(user: str, days: int, tz_name: str) -> ListeningPattern:
    client = LastFmClient(load_settings().lastfm_api_key)
    return collect_pattern(client, user, days, tz_name)


@st.cache_data(ttl=3600, show_spinner=False)
def load_timeline(user: str, recent_period: str, base_period: str) -> TimelineContext:
    settings = load_settings()
    musicbrainz = MusicBrainzClient(
        settings.musicbrainz_user_agent, cache=DiskCache(".cache/musicbrainz.json", ttl_seconds=30 * 86400)
    )
    return collect_timeline(LastFmClient(settings.lastfm_api_key), musicbrainz, user, recent_period, base_period)


@st.cache_data(ttl=3600, show_spinner=False)
def load_listened(user: str) -> list[str]:
    """이미 들은 아티스트(전체 기간 상위 500명). 이 범위 밖의 아티스트는 처방에서 걸러지지 않을 수 있다."""
    client = LastFmClient(load_settings().lastfm_api_key)
    return [artist.name for artist in client.get_top_artists(user, "overall", limit=LISTENED_LIMIT)]


def show_recommendations(result: RecommendationResult) -> None:
    for item in result.recommendations:
        st.markdown(f"**{item.artist} - {item.track}**")
        st.write(item.reason)
        details = [f"근거: {item.based_on}"] if item.based_on else []
        if item.url:
            details.append(f"[Last.fm에서 듣기]({item.url})")
        if details:
            st.caption(" · ".join(details))
        st.divider()
    show_tool_calls(result.tool_calls, result.hit_limit)


def show_tool_calls(calls: list[ToolCall], hit_limit: bool) -> None:
    if hit_limit:
        st.caption("도구 호출 한도에 닿아 지금까지 조회한 결과로 답했습니다.")
    if calls:
        with st.expander(f"평론가가 조회한 데이터 ({len(calls)}회)"):
            for call in calls:
                args = ", ".join(str(value) for value in call.input.values())
                st.markdown(f"- `{call.name}`({args})" + (" (실패)" if call.is_error else ""))


def show_qa_result(result: QAResult) -> None:
    with st.chat_message("user"):
        st.write(result.question)
    with st.chat_message("assistant"):
        st.markdown(f"**{result.answer.headline}**")
        st.write(result.answer.body)
        show_evidence(result.answer.evidence)
        show_tool_calls(result.tool_calls, result.hit_limit)


def show_evidence(evidence: list[str]) -> None:
    if evidence:
        with st.expander("근거 데이터"):
            for item in evidence:
                st.markdown(f"- {item}")


@st.cache_data(ttl=1800, show_spinner=False)
def load_chart_report(country: str | None, user: str) -> ChartReport:
    """user가 비어 있으면 개인 비교 없이 차트만 가져온다."""
    lastfm = LastFmClient(load_settings().lastfm_api_key)
    if not user:
        return collect_chart_report(lastfm, country)
    listened = load_listened(user)
    user_tags = [tag.name for tag in lastfm.get_top_tags(user, limit=30)]
    return collect_chart_report(lastfm, country, listened=listened, user_tags=user_tags)


def chart_frame(items: list, heard: set[str] | None) -> pd.DataFrame:
    rows = []
    for item in items:
        row = {"순위": item.rank, "이름": f"{item.artist} - {item.name}" if hasattr(item, "artist") else item.name}
        if getattr(item, "listeners", None) is not None:
            row["청취자"] = item.listeners
        if getattr(item, "playcount", None) is not None:
            row["재생"] = item.playcount
        if heard is not None:
            row["내가 듣는 중"] = "✓" if item.name.strip().casefold() in heard else ""
        rows.append(row)
    return pd.DataFrame(rows).set_index("순위")


def show_chart(report: ChartReport, insight: Insight) -> None:
    st.subheader(insight.headline)
    st.write(insight.body)
    heard = {name.strip().casefold() for name in report.fit.heard_artists} if report.fit else None
    if report.fit:
        columns = st.columns(2)
        columns[0].metric("내가 이미 듣는 상위 아티스트", f"{len(report.fit.heard_artists)}명", f"{report.fit.heard_share:.0%}", delta_color="off")
        if report.fit.shared_tags is not None and report.tags:
            columns[1].metric("겹치는 상위 태그", f"{len(report.fit.shared_tags)} / {report.fit.tags_compared}", delta_color="off")
    if report.country:
        st.caption(f"{report.scope_label} 상위 {report.compared_size}명 중 전 세계 차트에도 있는 아티스트 {report.global_overlap}명")
        if report.country_only_artists:
            st.markdown("**이 나라 차트에만 있는 아티스트**\n\n" + ", ".join(report.country_only_artists))
    st.markdown("**상위 아티스트**")
    st.dataframe(chart_frame(report.artists, heard))
    st.markdown("**상위 곡**")
    st.dataframe(chart_frame(report.tracks, None))
    if report.tags:
        st.markdown("**상위 태그**")
        st.dataframe(chart_frame(report.tags, None))
    st.caption("순위는 Last.fm이 제공한 순서이며, 청취자 수와 재생 수의 집계 기간은 알 수 없습니다.")
    show_evidence(insight.evidence)


def show_share_card(review: Review, context: ReviewContext) -> None:
    try:
        card = render_share_card(
            review.headline, review.score, SCORE_MAX, [artist.name for artist in context.artists], context.period_label
        )
    except CardFontError as exc:
        st.caption(f"공유 카드를 만들지 못했습니다. {exc}")
        return
    st.image(card, width=320)
    # on_click="ignore": 저장 버튼을 눌러도 앱이 다시 실행되어 리뷰가 사라지지 않게 한다.
    st.download_button("공유 카드 저장 (PNG)", card, "taste-critic.png", "image/png", on_click="ignore")


def show_review(review: Review, context: ReviewContext) -> None:
    st.subheader(review.headline)
    st.metric("점수", f"{review.score} / {SCORE_MAX}")
    st.write(review.body)
    show_evidence(review.evidence)
    show_share_card(review, context)


def show_pattern(pattern: ListeningPattern, insight: Insight) -> None:
    st.subheader(insight.headline)
    st.write(insight.body)
    caption = f"{pattern.first_played:%Y-%m-%d} ~ {pattern.last_played:%Y-%m-%d}, {pattern.total}곡, 타임존 {pattern.timezone}"
    if pattern.truncated:
        caption += " (조회 상한에 걸려 더 오래된 기록은 제외됨)"
    st.caption(caption)

    st.markdown("**시각별 청취**")
    hours = [f"{hour:02d}시" for hour in range(24)]
    st.bar_chart(pd.DataFrame({"곡 수": pattern.hourly}, index=pd.Index(hours, name="시각")))
    st.markdown("**요일별 청취**")
    st.bar_chart(pd.DataFrame({"곡 수": pattern.weekday}, index=pd.Index(WEEKDAY_LABELS, name="요일")))
    st.markdown("**시간대 구간**")
    columns = st.columns(len(TIME_BUCKETS))
    for column, (name, start, end), count in zip(columns, TIME_BUCKETS, pattern.bucket_counts.values()):
        column.metric(f"{name} ({start}~{end}시)", f"{count / pattern.total:.0%}", f"{count}곡", delta_color="off")
    show_evidence(insight.evidence)


def share_table(recent: dict[str, float], base: dict[str, float], recent_label: str, base_label: str, limit: int) -> pd.DataFrame:
    keys = sorted(set(recent) | set(base), key=lambda key: -(recent.get(key, 0) + base.get(key, 0)))[:limit]
    return pd.DataFrame({recent_label: [recent.get(k, 0) * 100 for k in keys], base_label: [base.get(k, 0) * 100 for k in keys]}, index=keys)


def show_timeline(context: TimelineContext, insight: Insight) -> None:
    recent, base = context.recent, context.base
    st.subheader(insight.headline)
    st.write(insight.body)
    st.caption(f"{recent.label}(최근)과 {base.label}(기준) 비교. 기준 기간에는 최근 기간이 포함됩니다.")

    st.markdown("**태그 비중 (%)**")
    st.bar_chart(share_table(recent.tag_shares, base.tag_shares, recent.label, base.label, 8))
    left, right = st.columns(2)
    left.markdown("**새로 등장한 아티스트**\n\n" + ("\n".join(f"- {n}" for n in context.new_artists) or "없음"))
    right.markdown("**상위에서 빠진 아티스트**\n\n" + ("\n".join(f"- {n}" for n in context.dropped_artists) or "없음"))

    st.markdown("**아티스트 활동 시작 시기 (%)**")
    decades = {k: v for k, v in recent.decade_shares.items()}, {k: v for k, v in base.decade_shares.items()}
    table = share_table(*decades, recent.label, base.label, 12).sort_index()
    st.bar_chart(table)
    st.caption(f"MusicBrainz에서 확인된 재생 수 비율: {recent.label} {recent.decade_coverage:.0%}, {base.label} {base.decade_coverage:.0%}. 곡의 발매 연도가 아니라 아티스트의 활동 시작 연도 기준입니다.")

    st.markdown("**아티스트 출신 국가 (%)**")
    countries = [{country_label(k): v for k, v in s.items()} for s in (recent.country_shares, base.country_shares)]
    st.bar_chart(share_table(*countries, recent.label, base.label, 8))
    st.caption(f"확인된 재생 수 비율: {recent.label} {recent.country_coverage:.0%}, {base.label} {base.country_coverage:.0%}")
    matched = sorted(set(recent.name_matched) | set(base.name_matched))
    if matched:
        st.caption("이름 검색으로만 매칭되어 국가와 시기가 틀릴 수 있는 아티스트: " + ", ".join(matched))
    show_evidence(insight.evidence)


def report_error(exc: Exception) -> None:
    if isinstance(exc, (ValueError, UsageLimitError)):
        st.warning(str(exc))
    elif isinstance(exc, InsufficientDataError):
        st.info(str(exc))
    elif isinstance(exc, (ConfigError, LastFmError, MusicBrainzError)):
        st.error(str(exc))
    elif isinstance(exc, ReviewError):
        st.error(f"{exc} 잠시 후 다시 시도해 주세요.")
    else:
        # Claude 호출 오류의 상세 내용에는 연결 정보가 섞일 수 있어 화면에 내보내지 않는다.
        st.error("Claude 호출 중 오류가 발생했습니다. .env의 ANTHROPIC_* 설정을 확인해 주세요.")


def tone_picker(key: str) -> str:
    return st.radio(
        "톤",
        list(TONES),
        index=list(TONES).index(DEFAULT_TONE),
        format_func=lambda option: TONES[option]["label"],
        horizontal=True,
        key=key,
    )


def review_tab() -> None:
    with st.form("review_form"):
        username = st.text_input("Last.fm 사용자명", help="공개 프로필이어야 합니다.", key="review_user")
        period = st.selectbox("기간", PERIODS, index=PERIODS.index("3month"), format_func=PERIOD_LABELS.get)
        tone = tone_picker("review_tone")
        submitted = st.form_submit_button("리뷰 쓰기")
    if not submitted:
        return
    try:
        user = validate_username(username)
        with st.spinner("청취 기록을 불러오는 중..."):
            context = load_context(user, period)
        use_ai_quota()
        with st.spinner("평론가가 리뷰를 쓰는 중..."):
            review = generate_review(context, ask_without_thinking, tone)
    except Exception as exc:
        report_error(exc)
    else:
        show_review(review, context)


def pattern_tab() -> None:
    with st.form("pattern_form"):
        username = st.text_input("Last.fm 사용자명", help="공개 프로필이어야 합니다.", key="pattern_user")
        days = st.selectbox("기간", list(PATTERN_DAYS), index=1, format_func=PATTERN_DAYS.get)
        tz_name = st.selectbox("타임존", TIMEZONES)
        tone = tone_picker("pattern_tone")
        submitted = st.form_submit_button("패턴 분석")
    if not submitted:
        return
    try:
        user = validate_username(username)
        with st.spinner("청취 기록을 불러오는 중... (기록이 많으면 시간이 걸립니다)"):
            pattern = load_pattern(user, days, tz_name)
        use_ai_quota()
        with st.spinner("평론가가 패턴을 해석하는 중..."):
            insight = generate_pattern_insight(pattern, ask_without_thinking, tone)
    except Exception as exc:
        report_error(exc)
    else:
        show_pattern(pattern, insight)


def timeline_tab() -> None:
    comparable = [p for p in PERIODS if p != "overall"]
    with st.form("timeline_form"):
        username = st.text_input("Last.fm 사용자명", help="공개 프로필이어야 합니다.", key="timeline_user")
        recent_period = st.selectbox("최근 기간", comparable, index=comparable.index("1month"), format_func=PERIOD_LABELS.get)
        base_period = st.selectbox("비교 기준 기간", PERIODS[1:], index=PERIODS[1:].index("12month"), format_func=PERIOD_LABELS.get)
        tone = tone_picker("timeline_tone")
        submitted = st.form_submit_button("변화 보기")
    if not submitted:
        return
    try:
        user = validate_username(username)
        with st.spinner("아티스트 정보를 모으는 중... (처음에는 1분 가까이 걸릴 수 있어요)"):
            context = load_timeline(user, recent_period, base_period)
        use_ai_quota()
        with st.spinner("평론가가 변화를 해석하는 중..."):
            insight = generate_timeline_insight(context, ask_without_thinking, tone)
    except Exception as exc:
        report_error(exc)
    else:
        show_timeline(context, insight)


def recommend_tab() -> None:
    from ai_helper import client  # ai_helper.py의 공용 Claude 클라이언트

    with st.form("recommend_form"):
        username = st.text_input("Last.fm 사용자명", help="공개 프로필이어야 합니다.", key="recommend_user")
        period = st.selectbox(
            "취향을 읽을 기간", PERIODS, index=PERIODS.index("3month"), format_func=PERIOD_LABELS.get, key="recommend_period"
        )
        tone = tone_picker("recommend_tone")
        submitted = st.form_submit_button("다음 음악 처방받기")
    if not submitted:
        return
    try:
        user = validate_username(username)
        with st.spinner("청취 기록을 불러오는 중..."):
            context = load_context(user, period)
            listened = load_listened(user)
        use_ai_quota()
        with st.spinner("평론가가 새로운 음악을 찾는 중... (10~30초)"):
            lastfm = LastFmClient(load_settings().lastfm_api_key)
            result = recommend(context, set(listened), lastfm, client, tone)
    except Exception as exc:
        report_error(exc)
    else:
        show_recommendations(result)
        st.caption(f"이미 들은 아티스트는 전체 기간 상위 {LISTENED_LIMIT}명 기준으로 제외했습니다.")


def qa_tab() -> None:
    from ai_helper import client  # ai_helper.py의 공용 Claude 클라이언트

    username = st.text_input("Last.fm 사용자명", help="공개 프로필이어야 합니다.", key="qa_user")
    tone = tone_picker("qa_tone")
    # 사용자명이 바뀌면 이전 대화는 다른 사람의 기록에 대한 것이므로 비운다.
    if st.session_state.get("qa_history_user") != username:
        st.session_state["qa_history_user"] = username
        st.session_state["qa_results"] = []
    results: list[QAResult] = st.session_state["qa_results"]
    for previous in results:
        show_qa_result(previous)

    question = st.chat_input("내 청취 기록에 대해 물어보세요. 예: 작년보다 밝은 음악을 듣고 있어?", key="qa_input")
    if not question:
        return
    try:
        user = validate_username(username)
        settings = load_settings()
        musicbrainz = MusicBrainzClient(
            settings.musicbrainz_user_agent, cache=DiskCache(".cache/musicbrainz.json", ttl_seconds=30 * 86400)
        )
        use_ai_quota()
        with st.spinner("평론가가 기록을 살펴보는 중..."):
            result = answer_question(
                client, LastFmClient(settings.lastfm_api_key), musicbrainz, user, question, [r.as_turn() for r in results], tone
            )
    except Exception as exc:
        with st.chat_message("user"):
            st.write(question)
        report_error(exc)
    else:
        results.append(result)
        show_qa_result(result)


def chart_tab() -> None:
    with st.form("chart_form"):
        country = st.selectbox(
            "차트 범위", [None, *COUNTRIES], format_func=lambda key: "전 세계" if key is None else f"{COUNTRIES[key]} ({key})"
        )
        username = st.text_input("내 취향과 비교 (선택)", help="Last.fm 사용자명을 넣으면 내가 이미 듣는 아티스트와 비교합니다.", key="chart_user")
        tone = tone_picker("chart_tone")
        submitted = st.form_submit_button("차트 분석")
    if not submitted:
        return
    try:
        user = validate_username(username) if username.strip() else ""
        with st.spinner("차트를 불러오는 중..."):
            report = load_chart_report(country, user)
        use_ai_quota()
        with st.spinner("평론가가 차트를 분석하는 중..."):
            insight = generate_chart_insight(report, ask_without_thinking, tone)
    except Exception as exc:
        report_error(exc)
    else:
        show_chart(report, insight)


def main() -> None:
    st.title("🎧 취향 평론가")
    st.caption("Last.fm 청취 기록을 바탕으로 Claude가 쓰는 나의 음악 취향 리뷰")
    review, pattern, timeline, recommendation, chart, chat = st.tabs(
        ["취향 리뷰", "청취 패턴", "취향 변화", "다음 음악", "차트", "대화"]
    )
    with review:
        review_tab()
    with pattern:
        pattern_tab()
    with timeline:
        timeline_tab()
    with recommendation:
        recommend_tab()
    with chart:
        chart_tab()
    with chat:
        qa_tab()
    st.divider()
    st.caption("음악 데이터 출처: [Last.fm](https://www.last.fm), [MusicBrainz](https://musicbrainz.org). 리뷰와 해석은 Claude가 작성했습니다.")


main()
