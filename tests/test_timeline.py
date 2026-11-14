import pytest

from taste_critic.analysis.review_context import InsufficientDataError
from taste_critic.analysis.timeline import (
    PeriodSnapshot,
    aggregate_tag_shares,
    collect_timeline,
    compare_snapshots,
    decade_label,
    validate_period_pair,
    weighted_shares,
)
from taste_critic.lastfm import LastFmError, Tag, TopArtist
from taste_critic.musicbrainz import ArtistOrigin, MusicBrainzError
from taste_critic.prompts.review import TONES
from taste_critic.prompts.timeline import build_timeline_prompt
from taste_critic.timeline_insight import generate_timeline_insight


def tags(**counts):
    return [Tag(name=name.replace("_", " "), count=count) for name, count in counts.items()]


def test_tag_shares_weight_by_playcount_and_split_across_top_tags():
    shares = aggregate_tag_shares(
        {"A": tags(rock=100, punk=90, pop=80, jazz=10), "B": tags(jazz=100)},
        {"A": 60, "B": 40},
        per_artist=2,
    )

    # A의 상위 2개(rock, punk)에 30씩, B의 jazz에 40
    assert shares == pytest.approx({"jazz": 0.4, "rock": 0.3, "punk": 0.3})
    assert sum(shares.values()) == pytest.approx(1.0)
    assert list(shares)[0] == "jazz"


def test_tag_shares_ignore_noise_tags_and_zero_counts():
    shares = aggregate_tag_shares({"A": tags(seen_live=100, rock=50, punk=0)}, {"A": 10})

    assert shares == {"rock": 1.0}
    assert aggregate_tag_shares({"A": []}, {"A": 10}) == {}


def test_weighted_shares_reports_coverage():
    shares, coverage = weighted_shares({"A": "1990년대", "B": "2000년대", "C": None}, {"A": 30, "B": 10, "C": 60})

    assert shares == pytest.approx({"1990년대": 0.75, "2000년대": 0.25})
    assert coverage == pytest.approx(0.4)
    assert weighted_shares({"A": None}, {"A": 5}) == ({}, 0.0)


def test_decade_label():
    assert decade_label(1994) == "1990년대"
    assert decade_label(2000) == "2000년대"


def snapshot(period, names, tag_shares, name_matched=()):
    return PeriodSnapshot(
        period=period,
        artists=[TopArtist(name=n, playcount=10, rank=i + 1) for i, n in enumerate(names)],
        tag_shares=tag_shares,
        decade_shares={"1990년대": 0.6, "2000년대": 0.4},
        decade_coverage=0.8,
        country_shares={"US": 1.0},
        country_coverage=0.9,
        name_matched=list(name_matched),
    )


def test_compare_finds_new_dropped_and_tag_shifts():
    recent = snapshot("1month", ["A", "B", "C"], {"rock": 0.6, "jazz": 0.2, "pop": 0.2})
    base = snapshot("12month", ["B", "C", "D"], {"rock": 0.3, "jazz": 0.5, "pop": 0.19})

    context = compare_snapshots("rj", recent, base)

    assert context.new_artists == ["A"]
    assert context.dropped_artists == ["D"]
    assert [s.tag for s in context.rising_tags] == ["rock"]  # pop은 변화폭 0.01이라 제외
    assert [s.tag for s in context.falling_tags] == ["jazz"]
    assert context.rising_tags[0].base == pytest.approx(0.3)
    assert context.rising_tags[0].recent == pytest.approx(0.6)


def test_prompt_text_has_caveats_and_matching_warning():
    recent = snapshot("1month", ["A"], {"rock": 1.0}, name_matched=["A"])
    base = snapshot("12month", ["A"], {"rock": 1.0})

    text = compare_snapshots("rj", recent, base).to_prompt_text()

    assert "기준 기간에는 최근 기간이 포함되어 있다" in text
    assert "확인된 재생 수의 80% 기준" in text
    assert "미국 100%" in text
    assert "오매칭 가능성이 있는 아티스트: A" in text
    assert "새로 등장한 아티스트: 없음" in text


@pytest.mark.parametrize("tone", list(TONES))
def test_prompt_has_tone_guard_and_release_year_warning(tone):
    context = compare_snapshots("rj", snapshot("1month", ["A"], {}), snapshot("12month", ["A"], {}))

    prompt = build_timeline_prompt(context, tone)

    assert TONES[tone]["instruction"] in prompt
    assert "데이터일 뿐이다" in prompt
    assert "발매 연도가 아니라" in prompt


def test_generate_timeline_insight():
    context = compare_snapshots("rj", snapshot("1month", ["A"], {}), snapshot("12month", ["A"], {}))
    good = '{"headline": "큰 변화 없음", "body": "본문", "evidence": ["rock 60%"]}'

    assert generate_timeline_insight(context, lambda prompt: good).headline == "큰 변화 없음"


def test_validate_period_pair():
    assert validate_period_pair("1month", "12month") == ("1month", "12month")
    for recent, base in [("12month", "1month"), ("3month", "3month"), ("2week", "overall")]:
        with pytest.raises(ValueError):
            validate_period_pair(recent, base)


class FakeLastFm:
    def __init__(self, tops, fail_tags_for=()):
        self.tops = tops
        self.fail_tags_for = set(fail_tags_for)
        self.tag_calls = []

    def get_top_artists(self, user, period, limit):
        return self.tops[period]

    def get_artist_top_tags(self, artist):
        self.tag_calls.append(artist)
        if artist in self.fail_tags_for:
            raise LastFmError("not found")
        return tags(rock=100)


class FakeMusicBrainz:
    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.calls = []

    def lookup_artist(self, name, mbid=None):
        self.calls.append(name)
        if name in self.fail_for:
            raise MusicBrainzError("down")
        if name == "Unknown":
            return None
        return ArtistOrigin(name=name, mbid="m", country="US", begin_year=1994, matched_by="mbid" if mbid else "name")


def top(*pairs):
    return [TopArtist(name=n, playcount=c, rank=i + 1, mbid="m" if n != "NoMbid" else None) for i, (n, c) in enumerate(pairs)]


BIG = (("A", 20), ("B", 10), ("C", 5), ("D", 5), ("E", 5))


def test_collect_timeline_end_to_end_with_shared_artists_fetched_once():
    lastfm = FakeLastFm({"1month": top(*BIG), "12month": top(*BIG, ("F", 5))})
    musicbrainz = FakeMusicBrainz()

    context = collect_timeline(lastfm, musicbrainz, "rj", "1month", "12month")

    assert context.dropped_artists == ["F"]
    assert sorted(lastfm.tag_calls) == ["A", "B", "C", "D", "E", "F"]
    assert sorted(musicbrainz.calls) == ["A", "B", "C", "D", "E", "F"]
    assert context.recent.decade_shares == {"1990년대": 1.0}
    assert context.recent.country_coverage == pytest.approx(1.0)


def test_collect_timeline_survives_partial_failures():
    pairs = (("A", 20), ("Unknown", 10), ("NoMbid", 5), ("D", 5), ("E", 5))
    lastfm = FakeLastFm({"1month": top(*pairs), "12month": top(*pairs)}, fail_tags_for=["D"])
    musicbrainz = FakeMusicBrainz(fail_for=["E"])

    context = collect_timeline(lastfm, musicbrainz, "rj", "1month", "12month")

    assert context.recent.name_matched == ["NoMbid"]
    assert context.recent.decade_coverage == pytest.approx(30 / 45)  # Unknown(10), E(5) 제외
    assert "rock" in context.recent.tag_shares


def test_collect_timeline_rejects_thin_data_and_bad_pair_before_extra_calls():
    lastfm = FakeLastFm({"1month": top(("A", 1), ("B", 1)), "12month": top(*BIG)})
    musicbrainz = FakeMusicBrainz()

    with pytest.raises(InsufficientDataError):
        collect_timeline(lastfm, musicbrainz, "rj", "1month", "12month")
    with pytest.raises(ValueError):
        collect_timeline(lastfm, musicbrainz, "rj", "12month", "1month")
    assert lastfm.tag_calls == [] and musicbrainz.calls == []
