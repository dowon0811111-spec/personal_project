"""Last.fm API 클라이언트 (읽기 전용, API 키만 사용).

모든 Last.fm 호출은 이 모듈을 거친다. 초당 호출 수를 제한하고, 일시 오류는
지수 백오프로 재시도한다. 오류 메시지에는 API 키가 들어가는 요청 URL을 넣지 않는다.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests

from .models import (
    ArtistInfo,
    ChartArtist,
    ChartTag,
    ChartTrack,
    Period,
    RecentTrack,
    RecentTracksPage,
    SimilarArtist,
    Tag,
    TagTopArtist,
    TopArtist,
    TopTrack,
    as_list,
    validate_country,
    validate_period,
)

API_URL = "https://ws.audioscrobbler.com/2.0/"

# 8: 일시적 처리 실패, 11: 서비스 오프라인, 16: 일시적 오류, 29: 호출 한도 초과
RETRYABLE_ERROR_CODES = frozenset({8, 11, 16, 29})
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
ERROR_USER_NOT_FOUND = 6


class LastFmError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def is_not_found(self) -> bool:
        return self.code == ERROR_USER_NOT_FOUND


def _error_from_response(status_code: int, payload: Any) -> tuple[LastFmError, bool]:
    code = payload.get("error") if isinstance(payload, dict) else None
    detail = payload.get("message") if isinstance(payload, dict) else None
    if code == ERROR_USER_NOT_FOUND:
        message = f"Last.fm에서 찾을 수 없습니다: {detail}"
    else:
        message = f"Last.fm 요청이 실패했습니다 (HTTP {status_code}, 코드 {code}): {detail}"
    retryable = status_code in RETRYABLE_STATUS_CODES or code in RETRYABLE_ERROR_CODES
    return LastFmError(message, code), retryable


class LastFmClient:
    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        min_interval: float = 0.2,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        timeout: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("Last.fm API 키가 비어 있습니다.")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._last_call = float("-inf")

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self._min_interval - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last_call = self._clock()

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        query = {
            "method": method,
            "api_key": self._api_key,
            "format": "json",
            **{key: value for key, value in params.items() if value is not None},
        }
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._session.get(API_URL, params=query, timeout=self._timeout)
            except requests.RequestException as exc:
                error = LastFmError(f"Last.fm 서버에 연결하지 못했습니다 ({type(exc).__name__}).")
                retryable = True
            else:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if response.ok and isinstance(payload, dict) and "error" not in payload:
                    return payload
                error, retryable = _error_from_response(response.status_code, payload)
            if not retryable or attempt >= self._max_retries:
                raise error
            self._sleep(self._backoff_base * 2**attempt)
            attempt += 1

    def get_top_artists(self, user: str, period: Period = "overall", limit: int = 50) -> list[TopArtist]:
        data = self._call("user.getTopArtists", user=user, period=validate_period(period), limit=limit)
        return [TopArtist.model_validate(raw) for raw in as_list(data.get("topartists", {}).get("artist"))]

    def get_top_tracks(self, user: str, period: Period = "overall", limit: int = 50) -> list[TopTrack]:
        data = self._call("user.getTopTracks", user=user, period=validate_period(period), limit=limit)
        return [TopTrack.model_validate(raw) for raw in as_list(data.get("toptracks", {}).get("track"))]

    def get_top_tags(self, user: str, limit: int = 50) -> list[Tag]:
        data = self._call("user.getTopTags", user=user, limit=limit)
        return [Tag.model_validate(raw) for raw in as_list(data.get("toptags", {}).get("tag"))]

    def get_recent_tracks(
        self,
        user: str,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        page: int = 1,
        limit: int = 200,
    ) -> RecentTracksPage:
        """최근 청취 기록 한 페이지. 지금 재생 중인 곡은 재생 시각이 없어 제외한다."""
        params = {"user": user, "from": from_ts, "to": to_ts, "page": page, "limit": limit}
        data = self._call("user.getRecentTracks", **params).get("recenttracks", {})
        attr = data.get("@attr", {})
        tracks = [
            RecentTrack.model_validate(raw)
            for raw in as_list(data.get("track"))
            if (raw.get("@attr") or {}).get("nowplaying") != "true"
        ]
        return RecentTracksPage(
            tracks=tracks,
            page=attr.get("page", page),
            total_pages=attr.get("totalPages", 0),
            total=attr.get("total", 0),
        )

    def iter_recent_tracks(
        self,
        user: str,
        *,
        from_ts: int | None = None,
        to_ts: int | None = None,
        max_pages: int = 10,
    ) -> Iterator[RecentTrack]:
        """여러 페이지에 걸친 청취 기록을 차례로 돌려준다. 호출 수를 max_pages로 제한한다."""
        page = 1
        while page <= max_pages:
            result = self.get_recent_tracks(user, from_ts=from_ts, to_ts=to_ts, page=page)
            yield from result.tracks
            if page >= result.total_pages:
                return
            page += 1

    def get_similar_artists(self, artist: str, limit: int = 20) -> list[SimilarArtist]:
        data = self._call("artist.getSimilar", artist=artist, limit=limit, autocorrect=1)
        return [SimilarArtist.model_validate(raw) for raw in as_list(data.get("similarartists", {}).get("artist"))]

    def get_artist_top_tags(self, artist: str) -> list[Tag]:
        data = self._call("artist.getTopTags", artist=artist, autocorrect=1)
        return [Tag.model_validate(raw) for raw in as_list(data.get("toptags", {}).get("tag"))]

    def get_artist_info(self, artist: str) -> ArtistInfo:
        data = self._call("artist.getInfo", artist=artist, autocorrect=1)
        return ArtistInfo.model_validate(data.get("artist"))

    def get_artist_top_tracks(self, artist: str, limit: int = 5) -> list[TopTrack]:
        data = self._call("artist.getTopTracks", artist=artist, limit=limit, autocorrect=1)
        return [TopTrack.model_validate(raw) for raw in as_list(data.get("toptracks", {}).get("track"))]

    def _ranked(self, model: type, raw_items: Any) -> list[Any]:
        """차트 응답에는 순위 필드가 없으므로 응답 순서를 순위로 쓴다."""
        return [model.model_validate({**raw, "rank": i}) for i, raw in enumerate(as_list(raw_items), start=1)]

    def get_chart_artists(self, limit: int = 50) -> list[ChartArtist]:
        data = self._call("chart.getTopArtists", limit=limit)
        return self._ranked(ChartArtist, data.get("artists", {}).get("artist"))

    def get_chart_tracks(self, limit: int = 50) -> list[ChartTrack]:
        data = self._call("chart.getTopTracks", limit=limit)
        return self._ranked(ChartTrack, data.get("tracks", {}).get("track"))

    def get_chart_tags(self, limit: int = 50) -> list[ChartTag]:
        data = self._call("chart.getTopTags", limit=limit)
        return self._ranked(ChartTag, data.get("tags", {}).get("tag"))

    def get_geo_artists(self, country: str, limit: int = 50) -> list[ChartArtist]:
        data = self._call("geo.getTopArtists", country=validate_country(country), limit=limit)
        return self._ranked(ChartArtist, data.get("topartists", {}).get("artist"))

    def get_geo_tracks(self, country: str, limit: int = 50) -> list[ChartTrack]:
        data = self._call("geo.getTopTracks", country=validate_country(country), limit=limit)
        return self._ranked(ChartTrack, data.get("tracks", {}).get("track"))

    def get_tag_top_artists(self, tag: str, limit: int = 20) -> list[TagTopArtist]:
        data = self._call("tag.getTopArtists", tag=tag, limit=limit)
        return [TagTopArtist.model_validate(raw) for raw in as_list(data.get("topartists", {}).get("artist"))]
