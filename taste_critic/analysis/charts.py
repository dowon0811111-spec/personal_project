"""Last.fm 차트 분석: 전 세계 또는 나라별 차트와 개인 취향을 비교한다.

겹침, 차이 계산은 순수 함수로 두고 LLM에게 시키지 않는다. collect_chart_report만 Last.fm을 부른다.
"""

from collections.abc import Collection, Sequence

from pydantic import BaseModel, ConfigDict

from taste_critic.lastfm import COUNTRIES, ChartArtist, ChartTag, ChartTrack, LastFmClient, validate_country

CHART_SIZE = 50  # 비교를 위해 가져오는 개수
SHOWN = 20  # 화면과 프롬프트에 쓰는 상위 개수
GLOBAL_LABEL = "전 세계"


def _key(name: str) -> str:
    return name.strip().casefold()


class PersonalFit(BaseModel):
    model_config = ConfigDict(frozen=True)

    heard_artists: list[str]  # 차트 상위 중 내가 이미 듣는 아티스트
    unheard_artists: list[str]  # 차트 상위 중 아직 듣지 않은 아티스트
    shared_tags: list[str] | None  # 내 상위 태그와 겹치는 차트 태그. 사용자 태그를 모르면 None(없음과 다르다)
    tags_compared: int

    @property
    def heard_share(self) -> float:
        total = len(self.heard_artists) + len(self.unheard_artists)
        return len(self.heard_artists) / total if total else 0.0


class ChartReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope_label: str
    country: str | None
    artists: list[ChartArtist]
    tracks: list[ChartTrack]
    tags: list[ChartTag]  # 나라별 차트에는 태그 차트가 없어 비어 있다
    country_only_artists: list[str]  # 나라 차트에는 있고 전 세계 차트 상위에는 없는 아티스트
    global_overlap: int  # 나라 차트 상위 중 전 세계 차트 상위에도 있는 아티스트 수
    compared_size: int
    fit: PersonalFit | None

    def to_prompt_text(self) -> str:
        def stats(item: ChartArtist | ChartTrack) -> str:
            parts = []
            if item.listeners is not None:
                parts.append(f"청취자 {item.listeners}")
            if item.playcount is not None:
                parts.append(f"재생 {item.playcount}")
            return f" ({', '.join(parts)})" if parts else ""

        lines = [
            f"범위: {self.scope_label} 차트 (Last.fm이 제공한 순서가 순위이며, 수치의 집계 기간은 알 수 없다)",
            "",
            f"[상위 아티스트 {len(self.artists)}명]",
            *(f"{a.rank}. {a.name}{stats(a)}" for a in self.artists),
            "",
            f"[상위 곡 {len(self.tracks)}곡]",
            *(f"{t.rank}. {t.artist} - {t.name}{stats(t)}" for t in self.tracks),
        ]
        if self.tags:
            lines += ["", "[상위 태그]", *(f"{t.rank}. {t.name}" for t in self.tags)]
        else:
            lines += ["", "[상위 태그] 이 범위에는 태그 차트가 없다."]
        if self.country:
            lines += [
                "",
                "[전 세계 차트와 비교]",
                f"{self.scope_label} 상위 {self.compared_size}명 중 전 세계 상위 {self.compared_size}명에도 있는 아티스트: {self.global_overlap}명",
                "이 나라 차트에만 있는 아티스트(상위 순): " + (", ".join(self.country_only_artists) or "없음"),
            ]
        if self.fit is not None:
            lines += [
                "",
                "[사용자의 취향과 비교] (코드가 계산한 값)",
                f"차트 상위 {len(self.fit.heard_artists) + len(self.fit.unheard_artists)}명 중 사용자가 이미 듣는 아티스트: "
                f"{len(self.fit.heard_artists)}명 ({self.fit.heard_share:.0%}) - " + (", ".join(self.fit.heard_artists) or "없음"),
                "아직 듣지 않은 아티스트: " + (", ".join(self.fit.unheard_artists) or "없음"),
            ]
            if self.tags and self.fit.shared_tags is not None:
                lines.append(
                    f"차트 상위 태그 {self.fit.tags_compared}개 중 사용자의 상위 태그와 겹치는 것: "
                    + (", ".join(self.fit.shared_tags) or "없음")
                )
        return "\n".join(lines)


def split_by_listened(artists: Sequence[ChartArtist], listened: Collection[str]) -> tuple[list[str], list[str]]:
    known = {_key(name) for name in listened}
    heard = [a.name for a in artists if _key(a.name) in known]
    unheard = [a.name for a in artists if _key(a.name) not in known]
    return heard, unheard


def compare_with_global(
    country_artists: Sequence[ChartArtist], global_artists: Sequence[ChartArtist]
) -> tuple[int, list[str]]:
    """(전 세계 차트와도 겹치는 수, 이 나라 차트에만 있는 아티스트)"""
    global_names = {_key(a.name) for a in global_artists}
    country_only = [a.name for a in country_artists if _key(a.name) not in global_names]
    return len(country_artists) - len(country_only), country_only


def shared_tags(chart_tags: Sequence[ChartTag], user_tags: Collection[str]) -> list[str]:
    known = {_key(name) for name in user_tags}
    return [t.name for t in chart_tags if _key(t.name) in known]


def collect_chart_report(
    lastfm: LastFmClient,
    country: str | None = None,
    *,
    listened: Collection[str] | None = None,
    user_tags: Collection[str] | None = None,
) -> ChartReport:
    """country가 None이면 전 세계 차트. listened를 주면 내 취향과의 비교를 함께 계산한다."""
    if country is None:
        artists = lastfm.get_chart_artists(CHART_SIZE)
        tracks = lastfm.get_chart_tracks(CHART_SIZE)
        tags = lastfm.get_chart_tags(SHOWN)
        overlap, country_only = 0, []
    else:
        country = validate_country(country)
        artists = lastfm.get_geo_artists(country, CHART_SIZE)
        tracks = lastfm.get_geo_tracks(country, CHART_SIZE)
        tags = []
        overlap, country_only = compare_with_global(artists, lastfm.get_chart_artists(CHART_SIZE))

    top_artists = artists[:SHOWN]
    fit = None
    if listened is not None:
        heard, unheard = split_by_listened(top_artists, listened)
        shared = shared_tags(tags, user_tags) if user_tags is not None else None
        fit = PersonalFit(heard_artists=heard, unheard_artists=unheard, shared_tags=shared, tags_compared=len(tags))
    return ChartReport(
        scope_label=COUNTRIES[country] if country else GLOBAL_LABEL,
        country=country,
        artists=top_artists,
        tracks=tracks[:SHOWN],
        tags=tags,
        country_only_artists=country_only[:10],
        global_overlap=overlap,
        compared_size=len(artists),
        fit=fit,
    )
