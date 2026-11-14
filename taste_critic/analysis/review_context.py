"""리뷰에 쓸 청취 데이터를 하나의 컨텍스트 객체로 정리한다."""

from pydantic import BaseModel, ConfigDict

from taste_critic.lastfm import LastFmClient, Period, Tag, TopArtist, TopTrack

PERIOD_LABELS: dict[str, str] = {
    "7day": "최근 7일",
    "1month": "최근 1개월",
    "3month": "최근 3개월",
    "6month": "최근 6개월",
    "12month": "최근 12개월",
    "overall": "전체 기간",
}

MIN_ARTISTS = 5
MIN_TOTAL_PLAYS = 30


class InsufficientDataError(Exception):
    """청취 기록이 너무 적어 리뷰를 쓸 수 없을 때 발생한다."""


class ReviewContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    user: str
    period: Period
    artists: list[TopArtist]
    tracks: list[TopTrack]
    tags: list[Tag]

    @property
    def period_label(self) -> str:
        return PERIOD_LABELS[self.period]

    @property
    def total_artist_plays(self) -> int:
        return sum(artist.playcount for artist in self.artists)

    @property
    def top_artist_share(self) -> float:
        """상위 1위 아티스트가 수집한 아티스트 재생 수에서 차지하는 비율."""
        total = self.total_artist_plays
        return self.artists[0].playcount / total if total else 0.0

    def to_prompt_text(self) -> str:
        lines = [
            f"기간: {self.period_label}",
            f"수집한 상위 아티스트 {len(self.artists)}명의 재생 수 합계: {self.total_artist_plays}회",
            f"1위 아티스트의 비중: {self.top_artist_share:.0%}",
            "",
            "[상위 아티스트] (순위. 이름, 재생 수)",
            *(f"{a.rank}. {a.name}, {a.playcount}회" for a in self.artists),
            "",
            "[상위 곡] (순위. 아티스트 - 곡명, 재생 수)",
            *(f"{t.rank}. {t.artist} - {t.name}, {t.playcount}회" for t in self.tracks),
            "",
            "[상위 태그] (전체 기간 기준이며 선택한 기간으로 좁혀지지 않음)",
            *(f"{tag.name} ({tag.count})" for tag in self.tags),
        ]
        return "\n".join(lines)


def check_enough_data(artists: list[TopArtist]) -> None:
    total = sum(artist.playcount for artist in artists)
    if len(artists) < MIN_ARTISTS or total < MIN_TOTAL_PLAYS:
        raise InsufficientDataError(
            f"청취 기록이 너무 적어 리뷰를 쓰기 어렵습니다 (아티스트 {len(artists)}명, 재생 {total}회). "
            f"더 긴 기간을 선택해 보세요. 최소 기준: 아티스트 {MIN_ARTISTS}명, 재생 {MIN_TOTAL_PLAYS}회."
        )


def collect_review_context(
    client: LastFmClient,
    user: str,
    period: Period,
    *,
    artist_limit: int = 15,
    track_limit: int = 15,
    tag_limit: int = 10,
) -> ReviewContext:
    artists = client.get_top_artists(user, period=period, limit=artist_limit)
    check_enough_data(artists)
    return ReviewContext(
        user=user,
        period=period,
        artists=artists,
        tracks=client.get_top_tracks(user, period=period, limit=track_limit),
        tags=client.get_top_tags(user, limit=tag_limit),
    )
