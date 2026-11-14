"""다음 음악 처방용 도구.

도구 결과에서 사용자가 이미 들은 아티스트를 코드가 먼저 걸러 낸다(프롬프트에만 의존하지 않는다).
Claude에게 보여 준 후보 아티스트와 대표곡은 state에 기록해 두고, 최종 추천을 검증할 때 쓴다.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from taste_critic.lastfm import LastFmClient

from .registry import Tool, ToolRefusal, ToolRegistry

RESULT_LIMIT = 10
TRACKS_PER_ARTIST = 5


def normalize(name: str) -> str:
    return name.strip().casefold()


@dataclass(frozen=True)
class TrackInfo:
    name: str
    url: str


@dataclass
class RecommendationState:
    """도구 결과로 Claude에게 실제로 보여 준 것들."""

    candidates: dict[str, str] = field(default_factory=dict)  # 정규화한 이름 -> 표시 이름
    top_tracks: dict[str, list[TrackInfo]] = field(default_factory=dict)


class ArtistArgs(BaseModel):
    artist: str = Field(min_length=1, max_length=100, description="아티스트 이름")


class TagArgs(BaseModel):
    tag: str = Field(min_length=1, max_length=50, description="장르나 분위기 태그 (예: indie rock)")


def build_recommend_tools(lastfm: LastFmClient, listened: set[str], state: RecommendationState) -> ToolRegistry:
    """listened는 normalize한 이름의 집합이다."""

    def remember(names: list[str]) -> list[str]:
        for name in names:
            state.candidates.setdefault(normalize(name), name)
        return names

    def similar(args: ArtistArgs) -> list[dict]:
        found = [a for a in lastfm.get_similar_artists(args.artist, limit=30) if normalize(a.name) not in listened]
        found = found[:RESULT_LIMIT]
        remember([a.name for a in found])
        return [{"artist": a.name, "match": round(a.match, 2)} for a in found]

    def tag_top(args: TagArgs) -> list[dict]:
        found = [a for a in lastfm.get_tag_top_artists(args.tag, limit=40) if normalize(a.name) not in listened]
        found = found[:RESULT_LIMIT]
        remember([a.name for a in found])
        return [{"artist": a.name} for a in found]

    def top_tracks(args: ArtistArgs) -> list[dict]:
        key = normalize(args.artist)
        if key in listened:
            raise ToolRefusal("사용자가 이미 들은 아티스트입니다. 다른 아티스트를 고르세요.")
        if key not in state.candidates:
            raise ToolRefusal("후보에 없는 아티스트입니다. get_similar_artists나 get_tag_top_artists 결과에 나온 아티스트만 조회하세요.")
        tracks = lastfm.get_artist_top_tracks(state.candidates[key], limit=TRACKS_PER_ARTIST)
        state.top_tracks[key] = [TrackInfo(name=t.name, url=t.url) for t in tracks]
        return [{"track": t.name} for t in tracks]

    return ToolRegistry(
        [
            Tool(
                "get_similar_artists",
                "기준 아티스트와 비슷한 아티스트를 찾는다. 사용자가 이미 들은 아티스트는 결과에서 제외된다.",
                ArtistArgs,
                similar,
            ),
            Tool(
                "get_tag_top_artists",
                "태그(장르나 분위기)에서 인기 있는 아티스트를 찾는다. 사용자가 이미 들은 아티스트는 결과에서 제외된다.",
                TagArgs,
                tag_top,
            ),
            Tool(
                "get_artist_top_tracks",
                "후보 아티스트의 대표곡을 조회한다. 추천할 곡은 반드시 이 결과에서 고른다.",
                ArtistArgs,
                top_tracks,
            ),
        ]
    )
