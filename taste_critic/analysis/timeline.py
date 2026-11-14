"""취향 변화 타임라인: 두 기간의 아티스트, 태그, 시대, 지역을 비교한다.

집계와 비교는 순수 함수로 두고, 마지막의 collect_timeline만 Last.fm과 MusicBrainz를 부른다.
Last.fm의 기간은 겹친다(예: 최근 1개월은 최근 12개월에 포함된다). 비교 대상은 "최근"과 "더 긴 기준 기간"이다.
"""

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from taste_critic.analysis.review_context import PERIOD_LABELS, check_enough_data
from taste_critic.lastfm import PERIODS, LastFmClient, LastFmError, Period, Tag, TopArtist
from taste_critic.musicbrainz import ArtistOrigin, MusicBrainzClient, MusicBrainzError

TAGS_PER_ARTIST = 3
MIN_TAG_SHIFT = 0.02
# 취향과 무관한 잡음 태그
IGNORED_TAGS = frozenset(
    {"seen live", "favorites", "favourites", "favorite", "favourite", "awesome", "love", "albums i own", "spotify"}
)

COUNTRY_NAMES = {
    "KR": "한국", "US": "미국", "GB": "영국", "JP": "일본", "CA": "캐나다", "AU": "호주", "DE": "독일",
    "FR": "프랑스", "SE": "스웨덴", "NO": "노르웨이", "FI": "핀란드", "IE": "아일랜드", "NZ": "뉴질랜드",
    "IT": "이탈리아", "ES": "스페인", "BR": "브라질", "NL": "네덜란드", "XW": "세계(복수 국가)",
}


def country_label(code: str) -> str:
    return COUNTRY_NAMES.get(code, code)


def decade_label(year: int) -> str:
    return f"{year // 10 * 10}년대"


def aggregate_tag_shares(
    artist_tags: Mapping[str, Sequence[Tag]], playcounts: Mapping[str, int], per_artist: int = TAGS_PER_ARTIST
) -> dict[str, float]:
    """아티스트별 상위 태그에 재생 수를 나눠 실어 태그별 비중(합 1.0)을 구한다."""
    weights: dict[str, float] = {}
    for artist, tags in artist_tags.items():
        usable = [t for t in sorted(tags, key=lambda t: -t.count) if t.count > 0 and t.name.casefold() not in IGNORED_TAGS]
        chosen = usable[:per_artist]
        for tag in chosen:
            key = tag.name.casefold()
            weights[key] = weights.get(key, 0.0) + playcounts.get(artist, 0) / len(chosen)
    total = sum(weights.values())
    if not total:
        return {}
    return dict(sorted(((tag, w / total) for tag, w in weights.items()), key=lambda item: -item[1]))


def weighted_shares(labels: Mapping[str, str | None], playcounts: Mapping[str, int]) -> tuple[dict[str, float], float]:
    """라벨이 확인된 아티스트만으로 비중을 구하고, 확인된 재생 수의 비율(커버리지)을 함께 돌려준다."""
    totals: dict[str, int] = {}
    known = 0
    for artist, label in labels.items():
        if label is not None:
            totals[label] = totals.get(label, 0) + playcounts.get(artist, 0)
            known += playcounts.get(artist, 0)
    everything = sum(playcounts.get(artist, 0) for artist in labels)
    shares = {label: count / known for label, count in totals.items()} if known else {}
    return shares, (known / everything if everything else 0.0)


class PeriodSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: Period
    artists: list[TopArtist]
    tag_shares: dict[str, float]
    decade_shares: dict[str, float]
    decade_coverage: float
    country_shares: dict[str, float]
    country_coverage: float
    name_matched: list[str]  # 이름 검색으로만 매칭돼 오매칭 가능성이 있는 아티스트

    @property
    def label(self) -> str:
        return PERIOD_LABELS[self.period]


class TagShift(BaseModel):
    model_config = ConfigDict(frozen=True)

    tag: str
    base: float
    recent: float


class TimelineContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    user: str
    recent: PeriodSnapshot
    base: PeriodSnapshot
    new_artists: list[str]  # 최근 상위에는 있고 기준 기간 상위에는 없는 아티스트
    dropped_artists: list[str]  # 기준 기간 상위에는 있고 최근 상위에는 없는 아티스트
    rising_tags: list[TagShift]
    falling_tags: list[TagShift]

    def to_prompt_text(self) -> str:
        return "\n\n".join(
            [
                f"비교: {self.recent.label}(최근)과 {self.base.label}(기준). 기준 기간에는 최근 기간이 포함되어 있다.",
                _snapshot_text("최근", self.recent),
                _snapshot_text("기준", self.base),
                "[변화]",
                "최근 상위에 새로 등장한 아티스트: " + (", ".join(self.new_artists) or "없음"),
                "기준 기간 상위에서 빠진 아티스트: " + (", ".join(self.dropped_artists) or "없음"),
                "비중이 커진 태그: " + (_shifts_text(self.rising_tags) or "없음"),
                "비중이 줄어든 태그: " + (_shifts_text(self.falling_tags) or "없음"),
            ]
        )


def _percent_text(shares: Mapping[str, float], limit: int, rename=lambda key: key) -> str:
    return ", ".join(f"{rename(key)} {share:.0%}" for key, share in list(shares.items())[:limit]) or "확인된 정보 없음"


def _shifts_text(shifts: Sequence[TagShift]) -> str:
    return ", ".join(f"{s.tag} ({s.base:.0%} → {s.recent:.0%})" for s in shifts)


def _snapshot_text(title: str, snapshot: PeriodSnapshot) -> str:
    artists = ", ".join(f"{a.name} {a.playcount}회" for a in snapshot.artists)
    lines = [
        f"[{title}: {snapshot.label}]",
        f"상위 아티스트: {artists}",
        f"태그 비중(상위 아티스트의 태그를 재생 수로 가중): {_percent_text(snapshot.tag_shares, 8)}",
        f"아티스트 활동 시작 연도 분포(확인된 재생 수의 {snapshot.decade_coverage:.0%} 기준): "
        + _percent_text(dict(sorted(snapshot.decade_shares.items())), 10),
        f"아티스트 출신 국가 분포(확인된 재생 수의 {snapshot.country_coverage:.0%} 기준): "
        + _percent_text(snapshot.country_shares, 6, country_label),
    ]
    if snapshot.name_matched:
        lines.append(f"이름 검색으로만 매칭되어 오매칭 가능성이 있는 아티스트: {', '.join(snapshot.name_matched)}")
    return "\n".join(lines)


def compare_snapshots(
    user: str, recent: PeriodSnapshot, base: PeriodSnapshot, *, tag_limit: int = 5
) -> TimelineContext:
    recent_names = {a.name for a in recent.artists}
    base_names = {a.name for a in base.artists}
    shifts = [
        TagShift(tag=tag, base=base.tag_shares.get(tag, 0.0), recent=recent.tag_shares.get(tag, 0.0))
        for tag in set(recent.tag_shares) | set(base.tag_shares)
    ]
    rising = sorted((s for s in shifts if s.recent - s.base >= MIN_TAG_SHIFT), key=lambda s: s.base - s.recent)
    falling = sorted((s for s in shifts if s.base - s.recent >= MIN_TAG_SHIFT), key=lambda s: s.recent - s.base)
    return TimelineContext(
        user=user,
        recent=recent,
        base=base,
        new_artists=[a.name for a in recent.artists if a.name not in base_names],
        dropped_artists=[a.name for a in base.artists if a.name not in recent_names],
        rising_tags=rising[:tag_limit],
        falling_tags=falling[:tag_limit],
    )


def validate_period_pair(recent: str, base: str) -> tuple[Period, Period]:
    if recent not in PERIODS or base not in PERIODS:
        raise ValueError(f"기간은 {', '.join(PERIODS)} 중 하나여야 합니다.")
    if PERIODS.index(recent) >= PERIODS.index(base):
        raise ValueError("비교 기간은 최근 기간보다 길어야 합니다.")
    return recent, base  # type: ignore[return-value]


def _build_snapshot(
    lastfm: LastFmClient,
    musicbrainz: MusicBrainzClient,
    period: Period,
    artists: list[TopArtist],
    tags_memo: dict[str, list[Tag]],
    origin_memo: dict[str, ArtistOrigin | None],
) -> PeriodSnapshot:
    playcounts = {a.name: a.playcount for a in artists}
    for artist in artists:
        if artist.name not in tags_memo:
            try:
                tags_memo[artist.name] = lastfm.get_artist_top_tags(artist.name)
            except LastFmError:
                tags_memo[artist.name] = []  # 태그를 못 가져온 아티스트는 태그 집계에서 빠진다.
        if artist.name not in origin_memo:
            try:
                origin_memo[artist.name] = musicbrainz.lookup_artist(artist.name, artist.mbid)
            except MusicBrainzError:
                origin_memo[artist.name] = None  # 일시 오류는 이번 실행에서만 "확인 안 됨"으로 본다.
    origins = {a.name: origin_memo[a.name] for a in artists}
    decade_shares, decade_cov = weighted_shares(
        {n: decade_label(o.begin_year) if o and o.begin_year else None for n, o in origins.items()}, playcounts
    )
    country_shares, country_cov = weighted_shares({n: o.country if o else None for n, o in origins.items()}, playcounts)
    return PeriodSnapshot(
        period=period,
        artists=artists,
        tag_shares=aggregate_tag_shares({a.name: tags_memo[a.name] for a in artists}, playcounts),
        decade_shares=decade_shares,
        decade_coverage=decade_cov,
        country_shares=dict(sorted(country_shares.items(), key=lambda item: -item[1])),
        country_coverage=country_cov,
        name_matched=[n for n, o in origins.items() if o and o.matched_by == "name"],
    )


def collect_timeline(
    lastfm: LastFmClient,
    musicbrainz: MusicBrainzClient,
    user: str,
    recent_period: str,
    base_period: str,
    *,
    top_n: int = 10,
) -> TimelineContext:
    recent_period, base_period = validate_period_pair(recent_period, base_period)
    recent_artists = lastfm.get_top_artists(user, period=recent_period, limit=top_n)
    check_enough_data(recent_artists)
    base_artists = lastfm.get_top_artists(user, period=base_period, limit=top_n)
    check_enough_data(base_artists)
    tags_memo: dict[str, list[Tag]] = {}
    origin_memo: dict[str, ArtistOrigin | None] = {}
    recent = _build_snapshot(lastfm, musicbrainz, recent_period, recent_artists, tags_memo, origin_memo)
    base = _build_snapshot(lastfm, musicbrainz, base_period, base_artists, tags_memo, origin_memo)
    return compare_snapshots(user, recent, base)
