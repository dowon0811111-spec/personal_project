"""MusicBrainz API 클라이언트 (아티스트의 국가와 활동 시작 연도 보강용).

초당 1회 이하로 호출하고(전역 락으로 직렬화), 식별 가능한 User-Agent를 보내며,
같은 아티스트는 캐시에서 꺼낸다. Last.fm의 mbid가 없으면 이름 검색으로 대체하는데,
이때는 오매칭 가능성이 있어 결과에 matched_by="name"으로 표시한다.
"""

import threading
import time
from collections.abc import Callable
from typing import Any, Literal

import requests
from pydantic import BaseModel, ConfigDict

from taste_critic.cache import DiskCache

API_URL = "https://musicbrainz.org/ws/2/"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MIN_NAME_SCORE = 90


class MusicBrainzError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ArtistOrigin(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    mbid: str
    country: str | None = None  # ISO 3166-1 코드 (예: KR, US)
    begin_year: int | None = None  # 활동 시작(결성, 데뷔) 연도
    matched_by: Literal["mbid", "name"]


def _parse_origin(raw: dict[str, Any], matched_by: Literal["mbid", "name"]) -> ArtistOrigin:
    begin = str((raw.get("life-span") or {}).get("begin") or "")
    return ArtistOrigin(
        name=raw["name"],
        mbid=raw["id"],
        country=raw.get("country") or None,
        begin_year=int(begin[:4]) if begin[:4].isdigit() else None,
        matched_by=matched_by,
    )


def _escape_query(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class MusicBrainzClient:
    def __init__(
        self,
        user_agent: str,
        *,
        cache: DiskCache | None = None,
        session: requests.Session | None = None,
        min_interval: float = 1.1,
        max_retries: int = 2,
        backoff_base: float = 2.0,
        timeout: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("MusicBrainz는 식별 가능한 User-Agent가 필요합니다.")
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._cache = cache
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

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._session.get(
                    API_URL + path,
                    params={"fmt": "json", **params},
                    headers=self._headers,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                error = MusicBrainzError(f"MusicBrainz 서버에 연결하지 못했습니다 ({type(exc).__name__}).")
                retryable = True
            else:
                if response.ok:
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        return payload
                    error, retryable = MusicBrainzError("MusicBrainz 응답을 해석하지 못했습니다."), False
                else:
                    error = MusicBrainzError(f"MusicBrainz 요청이 실패했습니다 (HTTP {response.status_code}).", response.status_code)
                    retryable = response.status_code in RETRYABLE_STATUS_CODES
            if not retryable or attempt >= self._max_retries:
                raise error
            self._sleep(self._backoff_base * 2**attempt)
            attempt += 1

    def _cached(self, key: str, fetch: Callable[[], ArtistOrigin | None]) -> ArtistOrigin | None:
        if self._cache is not None:
            found, value = self._cache.get(key)
            if found:
                return ArtistOrigin.model_validate(value) if value else None
        origin = fetch()
        if self._cache is not None:
            self._cache.set(key, origin.model_dump() if origin else None)
        return origin

    def _lookup_by_mbid(self, mbid: str) -> ArtistOrigin | None:
        try:
            return _parse_origin(self._get(f"artist/{mbid}"), "mbid")
        except MusicBrainzError as exc:
            if exc.status == 404:
                return None
            raise

    def _search_by_name(self, name: str) -> ArtistOrigin | None:
        data = self._get("artist", query=f'artist:"{_escape_query(name)}"', limit=3)
        for raw in data.get("artists", []):
            # 이름이 같고 검색 점수가 높은 첫 결과만 받아들인다. 애매하면 매칭하지 않는다.
            if raw.get("score", 0) >= MIN_NAME_SCORE and raw.get("name", "").casefold() == name.casefold():
                return _parse_origin(raw, "name")
        return None

    def lookup_artist(self, name: str, mbid: str | None = None) -> ArtistOrigin | None:
        """아티스트의 국가와 활동 시작 연도를 찾는다. 찾지 못하면 None.

        네트워크 오류는 MusicBrainzError로 올리고 캐시에 저장하지 않는다.
        """
        if mbid:
            origin = self._cached(f"mbid:{mbid}", lambda: self._lookup_by_mbid(mbid))
            if origin is not None:
                return origin
        return self._cached(f"name:{name.casefold()}", lambda: self._search_by_name(name))
