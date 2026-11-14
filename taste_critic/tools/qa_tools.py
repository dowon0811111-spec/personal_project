"""대화형 질문용 도구.

사용자명은 도구 인자가 아니라 빌드 시점에 고정한다. Claude가 다른 사용자의 기록을 조회할 수 없다.
결과는 Claude에 넘기기 전에 필요한 필드만 남겨 줄인다(원본 전체를 넘기지 않는다).
"""

from typing import Literal

from pydantic import BaseModel, Field

from taste_critic.analysis.patterns import DEFAULT_TIMEZONE, collect_pattern
from taste_critic.analysis.review_context import InsufficientDataError
from taste_critic.analysis.timeline import aggregate_tag_shares, country_label
from taste_critic.lastfm import COUNTRIES, LastFmClient, LastFmError, Period
from taste_critic.musicbrainz import MusicBrainzClient, MusicBrainzError

from .registry import Tool, ToolRefusal, ToolRegistry

TAG_SOURCE_ARTISTS = 10
TOP_TAGS_SHOWN = 8


class PeriodListArgs(BaseModel):
    period: Period = Field(description="조회 기간")
    limit: int = Field(default=10, ge=1, le=20, description="가져올 개수")


class PeriodArgs(BaseModel):
    period: Period = Field(description="조회 기간")


class PatternArgs(BaseModel):
    days: Literal[7, 30, 90] = Field(description="최근 며칠의 청취 기록을 볼지")


class ArtistArgs(BaseModel):
    artist: str = Field(min_length=1, max_length=100, description="아티스트 이름")


class ChartArgs(BaseModel):
    kind: Literal["artists", "tracks", "tags"] = Field(description="차트 종류")
    country: str | None = Field(
        default=None,
        description="비우면 전 세계 차트. 나라별 차트는 다음 중 하나의 영어 이름: " + ", ".join(COUNTRIES) + ". 나라별 차트에는 tags가 없다.",
    )
    limit: int = Field(default=10, ge=1, le=20, description="가져올 개수")


def build_qa_tools(
    lastfm: LastFmClient, musicbrainz: MusicBrainzClient, user: str, tz_name: str = DEFAULT_TIMEZONE
) -> ToolRegistry:
    tag_memo: dict[Period, dict] = {}

    def top_artists(args: PeriodListArgs) -> list[dict]:
        found = lastfm.get_top_artists(user, args.period, limit=args.limit)
        return [{"rank": a.rank, "artist": a.name, "plays": a.playcount} for a in found]

    def top_tracks(args: PeriodListArgs) -> list[dict]:
        found = lastfm.get_top_tracks(user, args.period, limit=args.limit)
        return [{"rank": t.rank, "artist": t.artist, "track": t.name, "plays": t.playcount} for t in found]

    def period_tags(args: PeriodArgs) -> dict:
        if args.period not in tag_memo:
            artists = lastfm.get_top_artists(user, args.period, limit=TAG_SOURCE_ARTISTS)
            artist_tags = {}
            for artist in artists:
                try:
                    artist_tags[artist.name] = lastfm.get_artist_top_tags(artist.name)
                except LastFmError:
                    continue  # 태그를 못 가져온 아티스트는 집계에서 뺀다.
            shares = aggregate_tag_shares(artist_tags, {a.name: a.playcount for a in artists})
            tag_memo[args.period] = {
                "based_on": f"상위 아티스트 {len(artist_tags)}명의 태그를 재생 수로 가중",
                "tags": [{"tag": tag, "share_pct": round(share * 100)} for tag, share in list(shares.items())[:TOP_TAGS_SHOWN]],
            }
        return tag_memo[args.period]

    def listening_pattern(args: PatternArgs) -> dict:
        try:
            pattern = collect_pattern(lastfm, user, args.days, tz_name)
        except InsufficientDataError as exc:
            raise ToolRefusal(str(exc)) from exc
        return {
            "timezone": pattern.timezone,
            "total_plays": pattern.total,
            "range": f"{pattern.first_played:%Y-%m-%d}~{pattern.last_played:%Y-%m-%d}",
            "time_of_day_share_pct": {name: round(count / pattern.total * 100) for name, count in pattern.bucket_counts.items()},
            "peak_hours": pattern.peak_hours,
            "plays_by_weekday": dict(zip("월화수목금토일", pattern.weekday)),
            "weekend_share_pct": round(pattern.weekend_share * 100),
            "truncated": pattern.truncated,
        }

    def chart(args: ChartArgs) -> list[dict]:
        if args.country is not None and args.country not in COUNTRIES:
            raise ToolRefusal("지원하지 않는 나라입니다. 설명에 적힌 영어 이름 중 하나를 쓰세요.")
        if args.kind == "tags":
            if args.country is not None:
                raise ToolRefusal("나라별 차트에는 태그 차트가 없습니다. 전 세계 차트에서만 조회할 수 있습니다.")
            return [{"rank": t.rank, "tag": t.name} for t in lastfm.get_chart_tags(args.limit)]
        if args.kind == "artists":
            found = lastfm.get_chart_artists(args.limit) if args.country is None else lastfm.get_geo_artists(args.country, args.limit)
            return [{"rank": a.rank, "artist": a.name, "listeners": a.listeners} for a in found]
        found = lastfm.get_chart_tracks(args.limit) if args.country is None else lastfm.get_geo_tracks(args.country, args.limit)
        return [{"rank": t.rank, "artist": t.artist, "track": t.name, "listeners": t.listeners} for t in found]

    def artist_info(args: ArtistArgs) -> dict:
        info = lastfm.get_artist_info(args.artist)
        return {"artist": info.name, "tags": info.tags[:5], "listeners": info.listeners}

    def artist_origin(args: ArtistArgs) -> dict:
        try:
            origin = musicbrainz.lookup_artist(args.artist)
        except MusicBrainzError as exc:
            raise ToolRefusal("MusicBrainz 조회에 실패했습니다.") from exc
        if origin is None:
            raise ToolRefusal("MusicBrainz에서 확실히 일치하는 아티스트를 찾지 못했습니다.")
        return {
            "artist": origin.name,
            "country": country_label(origin.country) if origin.country else None,
            "active_since_year": origin.begin_year,
            "matched_by": origin.matched_by,
        }

    return ToolRegistry(
        [
            Tool("get_top_artists", "사용자의 기간별 상위 아티스트와 재생 수.", PeriodListArgs, top_artists),
            Tool("get_top_tracks", "사용자의 기간별 상위 곡과 재생 수.", PeriodListArgs, top_tracks),
            Tool(
                "get_period_tags",
                "사용자가 그 기간에 들은 음악의 장르와 분위기 태그 비중(%). 상위 아티스트의 태그를 재생 수로 가중한 값이다. 기간끼리 비교할 때 쓴다.",
                PeriodArgs,
                period_tags,
            ),
            Tool(
                "get_listening_pattern",
                "최근 며칠간의 청취 시간대(새벽, 오전, 오후, 밤)와 요일 패턴.",
                PatternArgs,
                listening_pattern,
            ),
            Tool(
                "get_chart",
                "Last.fm의 전 세계 또는 나라별 인기 차트(아티스트, 곡, 태그). 사용자의 청취 기록이 아니라 모든 청취자의 차트다. 수치의 집계 기간은 알 수 없다.",
                ChartArgs,
                chart,
            ),
            Tool("get_artist_info", "특정 아티스트의 태그와 청취자 수.", ArtistArgs, artist_info),
            Tool(
                "get_artist_origin",
                "특정 아티스트의 출신 국가와 활동 시작 연도(MusicBrainz). 곡의 발매 연도가 아니다.",
                ArtistArgs,
                artist_origin,
            ),
        ]
    )
