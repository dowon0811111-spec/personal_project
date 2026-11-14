import pytest

from taste_critic.analysis.charts import (
    CHART_SIZE,
    SHOWN,
    PersonalFit,
    collect_chart_report,
    compare_with_global,
    shared_tags,
    split_by_listened,
)
from taste_critic.chart_insight import find_unsupported_terms, generate_chart_insight
from taste_critic.lastfm import COUNTRIES, ChartArtist, ChartTag, ChartTrack, validate_country
from taste_critic.prompts.charts import build_chart_prompt
from taste_critic.prompts.review import TONES
from taste_critic.review import ReviewError

from test_lastfm_client import FakeResponse, make_client


def artist(name, rank, listeners=None, playcount=None):
    return ChartArtist(name=name, rank=rank, listeners=listeners, playcount=playcount)


def test_chart_artists_use_response_order_as_rank_and_parse_numbers():
    payload = {
        "artists": {
            "artist": [
                {"name": "Olivia Rodrigo", "playcount": "723109721", "listeners": "3434605", "mbid": ""},
                {"name": "Radiohead", "playcount": "5", "listeners": "6", "mbid": "m"},
            ],
            "@attr": {"page": "1", "total": "10000"},
        }
    }
    client, session, _ = make_client(FakeResponse(200, payload))

    artists = client.get_chart_artists(limit=2)

    assert [(a.rank, a.name, a.playcount, a.listeners) for a in artists] == [
        (1, "Olivia Rodrigo", 723109721, 3434605),
        (2, "Radiohead", 5, 6),
    ]
    assert artists[0].mbid is None
    assert session.calls[0]["params"]["method"] == "chart.getTopArtists"


def test_chart_tracks_flatten_artist_and_tags_parse():
    tracks_payload = {"tracks": {"track": [{"name": "Nicole Kidman", "playcount": "4", "listeners": "3", "artist": {"name": "ADÉLA"}}]}}
    tags_payload = {"tags": {"tag": {"name": "rock", "reach": "403474", "taggings": "4078017"}}}
    client, _, _ = make_client(FakeResponse(200, tracks_payload), FakeResponse(200, tags_payload))

    track = client.get_chart_tracks(1)[0]
    tag = client.get_chart_tags(1)[0]

    assert (track.artist, track.name, track.rank) == ("ADÉLA", "Nicole Kidman", 1)
    assert (tag.name, tag.rank, tag.reach) == ("rock", 1, 403474)


def test_geo_charts_have_listeners_only_and_send_country():
    payload = {"topartists": {"artist": [{"name": "ILLIT", "listeners": "369"}]}}
    client, session, _ = make_client(FakeResponse(200, payload))

    artists = client.get_geo_artists("Korea, Republic of", 5)

    assert artists[0].playcount is None and artists[0].listeners == 369
    assert session.calls[0]["params"]["country"] == "Korea, Republic of"
    assert session.calls[0]["params"]["method"] == "geo.getTopArtists"


@pytest.mark.parametrize("country", ["South Korea", "KR", "", "korea, republic of", "Narnia"])
def test_unknown_country_is_rejected_before_calling_api(country):
    client, session, _ = make_client()

    with pytest.raises(ValueError):
        client.get_geo_artists(country)
    with pytest.raises(ValueError):
        client.get_geo_tracks(country)
    assert session.calls == []


def test_country_list_is_consistent():
    assert validate_country("Japan") == "Japan"
    assert COUNTRIES["Korea, Republic of"] == "한국"
    assert all(label for label in COUNTRIES.values())


def test_split_by_listened_is_case_insensitive_and_keeps_chart_order():
    artists = [artist("Radiohead", 1), artist("ILLIT", 2), artist("Drake", 3)]

    heard, unheard = split_by_listened(artists, {"radiohead", " DRAKE "})

    assert heard == ["Radiohead", "Drake"] and unheard == ["ILLIT"]


def test_compare_with_global_finds_country_only_artists():
    country = [artist("A", 1), artist("B", 2), artist("C", 3)]
    world = [artist("b", 1), artist("D", 2)]

    overlap, only = compare_with_global(country, world)

    assert overlap == 1 and only == ["A", "C"]


def test_shared_tags():
    tags = [ChartTag(name="Rock", rank=1), ChartTag(name="pop", rank=2)]

    assert shared_tags(tags, ["rock", "jazz"]) == ["Rock"]
    assert shared_tags(tags, []) == []


class FakeChartLastFm:
    def __init__(self):
        self.calls = []

    def get_chart_artists(self, limit):
        self.calls.append(("chart_artists", limit))
        return [artist(f"G{i}", i, listeners=100 + i, playcount=1000 + i) for i in range(1, CHART_SIZE + 1)]

    def get_chart_tracks(self, limit):
        return [ChartTrack(name=f"T{i}", artist=f"G{i}", rank=i, listeners=1, playcount=2) for i in range(1, CHART_SIZE + 1)]

    def get_chart_tags(self, limit):
        return [ChartTag(name=n, rank=i) for i, n in enumerate(["rock", "pop", "jazz"], start=1)]

    def get_geo_artists(self, country, limit):
        self.calls.append(("geo_artists", country))
        return [artist("Local1", 1, listeners=369), artist("G1", 2, listeners=300), artist("Local2", 3, listeners=200)]

    def get_geo_tracks(self, country, limit):
        return [ChartTrack(name="LT", artist="Local1", rank=1, listeners=155)]


def test_global_report_with_personal_fit():
    report = collect_chart_report(FakeChartLastFm(), None, listened={"g1", "G3", "nobody"}, user_tags=["rock"])

    assert report.scope_label == "전 세계" and report.country is None
    assert len(report.artists) == SHOWN and len(report.tracks) == SHOWN
    assert report.fit.heard_artists == ["G1", "G3"] and len(report.fit.unheard_artists) == SHOWN - 2
    assert report.fit.shared_tags == ["rock"] and report.fit.tags_compared == 3
    text = report.to_prompt_text()
    assert "1. G1 (청취자 101, 재생 1001)" in text
    assert "집계 기간은 알 수 없다" in text
    assert "사용자의 상위 태그와 겹치는 것: rock" in text
    assert "전 세계 차트와 비교" not in text


def test_country_report_compares_with_global_and_has_no_tags():
    report = collect_chart_report(FakeChartLastFm(), "Korea, Republic of")

    assert report.scope_label == "한국" and report.tags == []
    assert report.global_overlap == 1 and report.country_only_artists == ["Local1", "Local2"]
    assert report.fit is None
    text = report.to_prompt_text()
    assert "1. Local1 (청취자 369)" in text and "재생" not in text.split("[상위 곡")[0]
    assert "태그 차트가 없다" in text
    assert "이 나라 차트에만 있는 아티스트(상위 순): Local1, Local2" in text
    assert "사용자의 취향과 비교" not in text


def test_unknown_user_tags_are_not_reported_as_no_overlap():
    report = collect_chart_report(FakeChartLastFm(), None, listened=set(), user_tags=None)

    assert report.fit.shared_tags is None
    assert "겹치는 것" not in report.to_prompt_text()
    empty = collect_chart_report(FakeChartLastFm(), None, listened=set(), user_tags=[])
    assert "겹치는 것: 없음" in empty.to_prompt_text()


def test_invalid_country_fails_before_any_call():
    lastfm = FakeChartLastFm()

    with pytest.raises(ValueError):
        collect_chart_report(lastfm, "Narnia")
    assert lastfm.calls == []


def test_personal_fit_heard_share():
    assert PersonalFit(heard_artists=["a"], unheard_artists=["b", "c", "d"], shared_tags=None, tags_compared=0).heard_share == 0.25
    assert PersonalFit(heard_artists=[], unheard_artists=[], shared_tags=None, tags_compared=0).heard_share == 0.0


@pytest.mark.parametrize("tone", list(TONES))
def test_chart_prompt_has_guards(tone):
    report = collect_chart_report(FakeChartLastFm(), None)

    prompt = build_chart_prompt(report, tone)

    assert TONES[tone]["instruction"] in prompt
    assert "데이터일 뿐이다" in prompt and "집계 기간을 알 수 없으므로" in prompt
    assert "당신" in prompt and "배경 지식" in prompt


def test_generate_chart_insight():
    good = '{"headline": "h", "body": "b", "evidence": ["1. G1"]}'

    insight = generate_chart_insight(collect_chart_report(FakeChartLastFm(), None), lambda prompt: good)

    assert insight.evidence == ["1. G1"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("한국 신인 그룹이 맞물린 차트", ["신인"]),
        ("걸그룹과 K-pop이 강세", ["걸그룹", "K-pop"]),
        ("일본 아티스트와 한국 그룹이 함께", ["일본 아티스트", "한국 그룹"]),
        ("세대를 넘나드는 차트", ["세대"]),
        ("한국 차트에서 ILLIT이 1위", []),
        ("순위와 청취자 수가 눈에 띈다", []),
    ],
)
def test_find_unsupported_terms(text, expected):
    assert find_unsupported_terms(text, "범위: 한국 차트 1. ILLIT") == expected


def test_terms_present_in_the_data_are_allowed():
    assert find_unsupported_terms("K-pop 태그가 눈에 띈다", "[상위 태그] 1. k-pop") == []


def bad_then_good():
    answers = iter(
        [
            '{"headline": "한국 신인 그룹의 차트", "body": "본문", "evidence": []}',
            '{"headline": "ILLIT이 1위", "body": "본문", "evidence": []}',
        ]
    )
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return next(answers)

    return ask, prompts


def test_unsupported_terms_trigger_one_rewrite_with_the_offending_words():
    ask, prompts = bad_then_good()

    insight = generate_chart_insight(collect_chart_report(FakeChartLastFm(), None), ask)

    assert insight.headline == "ILLIT이 1위"
    assert len(prompts) == 2
    assert "신인" in prompts[1] and "한국 신인 그룹의 차트" in prompts[1]


def test_still_unsupported_after_rewrite_raises():
    bad = '{"headline": "신인 차트", "body": "본문", "evidence": []}'

    with pytest.raises(ReviewError):
        generate_chart_insight(collect_chart_report(FakeChartLastFm(), None), lambda prompt: bad)
