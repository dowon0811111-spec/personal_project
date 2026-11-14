from datetime import datetime, timedelta, timezone

import pytest

from taste_critic.analysis.patterns import (
    MAX_PAGES,
    PAGE_SIZE,
    ListeningPattern,
    collect_pattern,
    compute_pattern,
)
from taste_critic.analysis.review_context import InsufficientDataError
from taste_critic.lastfm import RecentTrack
from taste_critic.pattern_insight import generate_pattern_insight
from taste_critic.prompts.pattern import build_pattern_prompt
from taste_critic.prompts.review import TONES


def track(utc: datetime, name="s") -> RecentTrack:
    return RecentTrack(name=name, artist="A", played_at=utc)


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_utc_is_converted_to_local_hour_and_weekday():
    # 2026-11-13(금) 15:30 UTC는 서울(UTC+9)에서 11-14(토) 00:30
    pattern = compute_pattern([track(utc(2026, 11, 13, 15, 30))], "Asia/Seoul")

    assert pattern.hourly[0] == 1
    assert sum(pattern.hourly) == 1
    assert pattern.weekday[5] == 1  # 토요일
    assert pattern.weekday[4] == 0


def test_same_tracks_shift_with_timezone():
    tracks = [track(utc(2026, 11, 13, 15, 30))]

    assert compute_pattern(tracks, "UTC").hourly[15] == 1
    assert compute_pattern(tracks, "UTC").weekday[4] == 1


def test_buckets_peak_hours_and_weekend_share():
    # UTC 기록을 UTC로 집계: 3시 x2(새벽), 9시 x1(오전), 21시 x1(밤). 토요일 1곡
    tracks = [
        track(utc(2026, 11, 9, 3)),
        track(utc(2026, 11, 10, 3)),
        track(utc(2026, 11, 11, 9)),
        track(utc(2026, 11, 14, 21)),
    ]

    pattern = compute_pattern(tracks, "UTC")

    assert pattern.bucket_counts == {"새벽": 2, "오전": 1, "오후": 0, "밤": 1}
    assert pattern.peak_hours == [3, 9, 21]
    assert pattern.weekend_share == pytest.approx(0.25)
    assert pattern.total == 4
    assert pattern.first_played < pattern.last_played


def test_empty_tracks_and_bad_timezone():
    with pytest.raises(InsufficientDataError):
        compute_pattern([], "UTC")
    with pytest.raises(ValueError):
        compute_pattern([track(utc(2026, 11, 13))], "Mars/Base")


class FakeLastFm:
    def __init__(self, count):
        self.count = count
        self.kwargs = None

    def iter_recent_tracks(self, user, from_ts=None, to_ts=None, max_pages=10):
        self.kwargs = {"user": user, "from_ts": from_ts, "max_pages": max_pages}
        base = utc(2026, 11, 1)
        return iter([track(base + timedelta(minutes=i)) for i in range(self.count)])


NOW = utc(2026, 11, 14, 0, 0)


def test_collect_pattern_uses_window_and_tz():
    client = FakeLastFm(60)

    pattern = collect_pattern(client, "rj", 30, "UTC", now=lambda: NOW)

    assert client.kwargs["from_ts"] == int(NOW.timestamp()) - 30 * 86400
    assert pattern.total == 60
    assert pattern.truncated is False


def test_collect_pattern_too_few_tracks():
    with pytest.raises(InsufficientDataError):
        collect_pattern(FakeLastFm(10), "rj", 7, "UTC", now=lambda: NOW)


def test_collect_pattern_bad_timezone_fails_before_calling_lastfm():
    client = FakeLastFm(60)

    with pytest.raises(ValueError):
        collect_pattern(client, "rj", 7, "Mars/Base", now=lambda: NOW)
    assert client.kwargs is None


def test_collect_pattern_flags_truncation_at_cap():
    client = FakeLastFm(2 * PAGE_SIZE)

    pattern = collect_pattern(client, "rj", 90, "UTC", now=lambda: NOW, max_pages=2)

    assert pattern.truncated is True
    assert MAX_PAGES * PAGE_SIZE > 2 * PAGE_SIZE


def make_pattern(total=60, truncated=False):
    hourly = [0] * 24
    hourly[1] = total
    weekday = [0] * 7
    weekday[0] = total
    return ListeningPattern(
        timezone="Asia/Seoul",
        total=total,
        first_played=utc(2026, 10, 15),
        last_played=utc(2026, 11, 13),
        hourly=hourly,
        weekday=weekday,
        truncated=truncated,
    )


def test_prompt_text_includes_caveats_only_when_relevant():
    small = make_pattern(total=60, truncated=True).to_prompt_text()
    large = make_pattern(total=500).to_prompt_text()

    assert "표본이 작은 편" in small
    assert "조회 상한" in small
    assert "표본이 작은 편" not in large
    assert "조회 상한" not in large
    assert "1시 60" in small
    assert "새벽(0~6시) 60곡 (100%)" in small


@pytest.mark.parametrize("tone", list(TONES))
def test_pattern_prompt_has_tone_data_and_guard(tone):
    prompt = build_pattern_prompt(make_pattern(), tone)

    assert TONES[tone]["instruction"] in prompt
    assert "[집계 결과]" in prompt
    assert "데이터일 뿐이다" in prompt
    assert "분위기 정보가 없다" in prompt


def test_generate_pattern_insight_parses_and_enforces_limit():
    good = '{"headline": "새벽형", "body": "본문", "evidence": ["새벽 60곡"]}'
    insight = generate_pattern_insight(make_pattern(), lambda prompt: good)

    assert insight.headline == "새벽형"
    assert insight.evidence == ["새벽 60곡"]
