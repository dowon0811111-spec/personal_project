"""Last.fm API 응답 모델.

Last.fm 응답은 숫자를 문자열로 주고("playcount": "123"), 항목이 하나뿐이면 리스트 대신
객체 하나를 주며, 없는 MBID는 빈 문자열로 준다. 이런 형태를 여기서 한 번에 정리해
나머지 코드는 깔끔한 타입만 다루게 한다.
"""

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

Period = Literal["7day", "1month", "3month", "6month", "12month", "overall"]
PERIODS: tuple[Period, ...] = ("7day", "1month", "3month", "6month", "12month", "overall")

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def as_list(value: Any) -> list[Any]:
    """Last.fm이 항목 하나를 객체로 줄 때도 리스트로 다룰 수 있게 한다."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def validate_username(raw: str) -> str:
    """사용자가 입력한 Last.fm 사용자명을 검증하고 앞뒤 공백을 없앤다."""
    username = raw.strip()
    if not _USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Last.fm 사용자명은 2~32자의 영문, 숫자, '_', '-', '.'만 쓸 수 있습니다.")
    return username


# Last.fm geo 차트가 받는 나라 이름(ISO 3166-1 표기)과 한국어 표시. 실제 API로 모두 확인했다.
COUNTRIES: dict[str, str] = {
    "Korea, Republic of": "한국",
    "Japan": "일본",
    "United States": "미국",
    "United Kingdom": "영국",
    "Germany": "독일",
    "France": "프랑스",
    "Canada": "캐나다",
    "Australia": "호주",
    "Brazil": "브라질",
    "Mexico": "멕시코",
    "India": "인도",
    "Indonesia": "인도네시아",
    "Italy": "이탈리아",
    "Spain": "스페인",
    "Netherlands": "네덜란드",
    "Sweden": "스웨덴",
    "Norway": "노르웨이",
    "Finland": "핀란드",
    "Poland": "폴란드",
    "Turkey": "튀르키예",
    "Taiwan": "대만",
    "Thailand": "태국",
    "Philippines": "필리핀",
    "Argentina": "아르헨티나",
    "Ireland": "아일랜드",
    "New Zealand": "뉴질랜드",
}


def validate_country(country: str) -> str:
    if country not in COUNTRIES:
        raise ValueError(f"지원하지 않는 나라입니다: {country}")
    return country


def validate_period(period: str) -> Period:
    if period not in PERIODS:
        raise ValueError(f"기간은 {', '.join(PERIODS)} 중 하나여야 합니다.")
    return period  # type: ignore[return-value]


class _LastFmModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    @field_validator("mbid", mode="before", check_fields=False)
    @classmethod
    def _blank_mbid_to_none(cls, value: Any) -> Any:
        return value or None


def _with_rank(raw: Any) -> Any:
    if isinstance(raw, dict) and "rank" not in raw:
        return {**raw, "rank": (raw.get("@attr") or {}).get("rank")}
    return raw


def _artist_name(artist: Any) -> Any:
    # 응답 종류에 따라 아티스트가 {"name": ...}, {"#text": ...}, 문자열 중 하나로 온다.
    if isinstance(artist, dict):
        return artist.get("name") or artist.get("#text")
    return artist


class TopArtist(_LastFmModel):
    name: str
    playcount: int
    rank: int
    mbid: str | None = None
    url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, raw: Any) -> Any:
        return _with_rank(raw)


class TopTrack(_LastFmModel):
    name: str
    artist: str
    playcount: int
    rank: int
    mbid: str | None = None
    url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, raw: Any) -> Any:
        raw = _with_rank(raw)
        if isinstance(raw, dict):
            raw = {**raw, "artist": _artist_name(raw.get("artist"))}
        return raw


class Tag(_LastFmModel):
    name: str
    count: int = 0


class RecentTrack(_LastFmModel):
    name: str
    artist: str
    album: str = ""
    played_at: datetime
    mbid: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        album = raw.get("album")
        uts = (raw.get("date") or {}).get("uts")
        played_at = raw.get("played_at")
        if played_at is None and uts:
            played_at = datetime.fromtimestamp(int(uts), tz=timezone.utc)
        return {
            **raw,
            "artist": _artist_name(raw.get("artist")),
            "album": album.get("#text", "") if isinstance(album, dict) else (album or ""),
            "played_at": played_at,
        }


class RecentTracksPage(_LastFmModel):
    tracks: list[RecentTrack]
    page: int
    total_pages: int
    total: int


class SimilarArtist(_LastFmModel):
    name: str
    match: float
    mbid: str | None = None
    url: str = ""


class TagTopArtist(_LastFmModel):
    name: str
    rank: int
    mbid: str | None = None
    url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, raw: Any) -> Any:
        return _with_rank(raw)


class ChartArtist(_LastFmModel):
    """차트의 아티스트. 순위는 응답 순서다. 나라별 차트(geo)에는 재생 수가 없다."""

    name: str
    rank: int
    listeners: int | None = None
    playcount: int | None = None
    mbid: str | None = None
    url: str = ""


class ChartTrack(_LastFmModel):
    name: str
    artist: str
    rank: int
    listeners: int | None = None
    playcount: int | None = None
    mbid: str | None = None
    url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, raw: Any) -> Any:
        if isinstance(raw, dict):
            return {**raw, "artist": _artist_name(raw.get("artist"))}
        return raw


class ChartTag(_LastFmModel):
    name: str
    rank: int
    reach: int | None = None  # 이 태그를 쓴 사용자 수
    taggings: int | None = None  # 이 태그가 붙은 횟수


class ArtistInfo(_LastFmModel):
    name: str
    mbid: str | None = None
    url: str = ""
    listeners: int = 0
    playcount: int = 0
    tags: list[str] = []
    similar: list[str] = []
    summary: str = ""

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, raw: Any) -> Any:
        if not isinstance(raw, dict):
            return raw
        flattened = dict(raw)
        # API 응답(중첩 형태)일 때만 펼친다. 이미 평평한 값은 그대로 둔다.
        if isinstance(raw.get("stats"), dict):
            flattened["listeners"] = raw["stats"].get("listeners", 0)
            flattened["playcount"] = raw["stats"].get("playcount", 0)
        if isinstance(raw.get("tags"), dict):
            flattened["tags"] = [tag["name"] for tag in as_list(raw["tags"].get("tag"))]
        if isinstance(raw.get("similar"), dict):
            flattened["similar"] = [artist["name"] for artist in as_list(raw["similar"].get("artist"))]
        if isinstance(raw.get("bio"), dict):
            flattened["summary"] = _HTML_TAG_PATTERN.sub("", raw["bio"].get("summary") or "").strip()
        return flattened
