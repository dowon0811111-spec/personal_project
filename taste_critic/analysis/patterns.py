"""청취 기록의 시간대/요일 패턴 집계 (순수 함수).

Last.fm은 재생 시각을 UTC로 주므로, 사용자의 타임존으로 바꾼 뒤 센다.
"""

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

from taste_critic.analysis.review_context import InsufficientDataError
from taste_critic.lastfm import LastFmClient, RecentTrack

DEFAULT_TIMEZONE = "Asia/Seoul"
WEEKDAY_LABELS = ("월", "화", "수", "목", "금", "토", "일")
# 시간대 구간: (이름, 시작 시, 끝 시(미포함))
TIME_BUCKETS = (("새벽", 0, 6), ("오전", 6, 12), ("오후", 12, 18), ("밤", 18, 24))

PAGE_SIZE = 200
MAX_PAGES = 10
MIN_TRACKS = 50
SMALL_SAMPLE = 200


class ListeningPattern(BaseModel):
    model_config = ConfigDict(frozen=True)

    timezone: str
    total: int
    first_played: datetime
    last_played: datetime
    hourly: list[int]  # 길이 24, 0시~23시
    weekday: list[int]  # 길이 7, 월요일부터
    truncated: bool = False  # 조회 상한에 걸려 더 오래된 기록이 빠졌는지

    @property
    def span_days(self) -> int:
        return max(1, (self.last_played - self.first_played).days + 1)

    @property
    def is_small_sample(self) -> bool:
        return self.total < SMALL_SAMPLE

    @property
    def bucket_counts(self) -> dict[str, int]:
        return {name: sum(self.hourly[start:end]) for name, start, end in TIME_BUCKETS}

    @property
    def peak_hours(self) -> list[int]:
        """재생이 많은 시간대 상위 3개 (동률이면 이른 시각 먼저)."""
        ranked = sorted(range(24), key=lambda hour: (-self.hourly[hour], hour))
        return [hour for hour in ranked[:3] if self.hourly[hour] > 0]

    @property
    def weekend_share(self) -> float:
        return (self.weekday[5] + self.weekday[6]) / self.total if self.total else 0.0

    def to_prompt_text(self) -> str:
        buckets = ", ".join(
            f"{name}({start}~{end}시) {count}곡 ({count / self.total:.0%})"
            for (name, start, end), count in zip(TIME_BUCKETS, self.bucket_counts.values())
        )
        lines = [
            f"타임존: {self.timezone}",
            f"분석 대상: 최근 청취 기록 {self.total}곡 ({self.first_played:%Y-%m-%d}~{self.last_played:%Y-%m-%d}, {self.span_days}일)",
            *(["주의: 조회 상한에 걸려 더 오래된 기록은 포함되지 않았다."] if self.truncated else []),
            *(["주의: 표본이 작은 편이다."] if self.is_small_sample else []),
            f"시간대 구간별: {buckets}",
            "재생이 가장 많은 시각: " + ", ".join(f"{hour}시({self.hourly[hour]}곡)" for hour in self.peak_hours),
            "시각별 재생 수: " + ", ".join(f"{hour}시 {count}" for hour, count in enumerate(self.hourly)),
            "요일별 재생 수: " + ", ".join(f"{label} {count}" for label, count in zip(WEEKDAY_LABELS, self.weekday)),
            f"주말(토,일) 비중: {self.weekend_share:.0%}",
        ]
        return "\n".join(lines)


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"알 수 없는 타임존입니다: {name}") from exc


def compute_pattern(
    tracks: Iterable[RecentTrack], tz_name: str = DEFAULT_TIMEZONE, *, truncated: bool = False
) -> ListeningPattern:
    tz = resolve_timezone(tz_name)
    hourly = [0] * 24
    weekday = [0] * 7
    played = []
    for track in tracks:
        local = track.played_at.astimezone(tz)
        hourly[local.hour] += 1
        weekday[local.weekday()] += 1
        played.append(local)
    if not played:
        raise InsufficientDataError("분석할 청취 기록이 없습니다.")
    return ListeningPattern(
        timezone=tz_name,
        total=len(played),
        first_played=min(played),
        last_played=max(played),
        hourly=hourly,
        weekday=weekday,
        truncated=truncated,
    )


def collect_pattern(
    client: LastFmClient,
    user: str,
    days: int,
    tz_name: str = DEFAULT_TIMEZONE,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    max_pages: int = MAX_PAGES,
) -> ListeningPattern:
    resolve_timezone(tz_name)  # 잘못된 타임존이면 Last.fm을 부르기 전에 실패시킨다.
    from_ts = int(now().timestamp()) - days * 86400
    tracks = list(client.iter_recent_tracks(user, from_ts=from_ts, max_pages=max_pages))
    if len(tracks) < MIN_TRACKS:
        raise InsufficientDataError(
            f"최근 {days}일 청취 기록이 {len(tracks)}곡뿐이라 패턴을 보기 어렵습니다. 더 긴 기간을 선택해 보세요. "
            f"(최소 {MIN_TRACKS}곡)"
        )
    return compute_pattern(tracks, tz_name, truncated=len(tracks) >= max_pages * PAGE_SIZE)
