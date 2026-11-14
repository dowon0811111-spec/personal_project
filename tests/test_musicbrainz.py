import pytest
import requests

from taste_critic.cache import DiskCache
from taste_critic.musicbrainz import MusicBrainzClient, MusicBrainzError
from taste_critic.musicbrainz.client import API_URL

UA = "taste-critic/0.1 (test@example.com)"


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(*responses, cache=None, min_interval=0.0, max_retries=2):
    session = FakeSession(*responses)
    fake_time = FakeTime()
    client = MusicBrainzClient(
        UA, cache=cache, session=session, min_interval=min_interval, max_retries=max_retries,
        sleep=fake_time.sleep, clock=fake_time.clock,
    )
    return client, session, fake_time


ARTIST = {"id": "abc-123", "name": "Green Day", "country": "US", "life-span": {"begin": "1987-03-01"}}


def search(*artists):
    return FakeResponse(200, {"artists": list(artists)})


def test_lookup_by_mbid_parses_country_and_year_and_sends_user_agent():
    client, session, _ = make_client(FakeResponse(200, ARTIST))

    origin = client.lookup_artist("Green Day", "abc-123")

    assert (origin.country, origin.begin_year, origin.matched_by) == ("US", 1987, "mbid")
    call = session.calls[0]
    assert call["url"] == API_URL + "artist/abc-123"
    assert call["headers"]["User-Agent"] == UA
    assert call["params"]["fmt"] == "json"


def test_year_only_and_missing_dates_and_country():
    raw = {"id": "x", "name": "Band", "life-span": {"begin": "1994"}}
    client, _, _ = make_client(FakeResponse(200, raw))
    origin = client.lookup_artist("Band", "x")
    assert (origin.country, origin.begin_year) == (None, 1994)

    client, _, _ = make_client(FakeResponse(200, {"id": "y", "name": "Band2"}))
    assert client.lookup_artist("Band2", "y").begin_year is None


def test_name_search_accepts_exact_high_score_match():
    client, session, _ = make_client(search({**ARTIST, "score": 100}))

    origin = client.lookup_artist("green day")

    assert origin.matched_by == "name"
    assert session.calls[0]["params"]["query"] == 'artist:"green day"'


@pytest.mark.parametrize(
    "result",
    [{**ARTIST, "score": 70}, {**ARTIST, "name": "Green Day Tribute", "score": 100}],
)
def test_name_search_rejects_low_score_or_different_name(result):
    client, _, _ = make_client(search(result))

    assert client.lookup_artist("Green Day") is None


def test_name_search_escapes_quotes():
    client, session, _ = make_client(search())

    client.lookup_artist('Say "Hi"\\')

    assert session.calls[0]["params"]["query"] == 'artist:"Say \\"Hi\\"\\\\"'


def test_mbid_404_falls_back_to_name_search():
    client, session, _ = make_client(FakeResponse(404, {}), search({**ARTIST, "score": 100}))

    origin = client.lookup_artist("Green Day", "stale-id")

    assert origin.matched_by == "name"
    assert len(session.calls) == 2


def test_results_are_cached_including_not_found():
    cache = DiskCache.__new__(DiskCache)  # 파일 없이 메모리처럼 쓰기 위한 대체
    store = {}
    cache.get = lambda key: (key in store, store.get(key))
    cache.set = lambda key, value: store.__setitem__(key, value)
    client, session, _ = make_client(search({**ARTIST, "score": 100}), search(), cache=cache)

    first = client.lookup_artist("Green Day")
    again = client.lookup_artist("Green Day")
    assert first == again
    assert client.lookup_artist("Nobody Band") is None
    assert client.lookup_artist("Nobody Band") is None
    assert len(session.calls) == 2


def test_retry_with_backoff_then_success():
    client, session, fake_time = make_client(FakeResponse(503, {}), FakeResponse(503, {}), FakeResponse(200, ARTIST))

    assert client.lookup_artist("Green Day", "abc-123") is not None
    assert fake_time.sleeps == [2.0, 4.0]
    assert len(session.calls) == 3


def test_gives_up_and_error_is_not_cached():
    store = {}
    cache = DiskCache.__new__(DiskCache)
    cache.get = lambda key: (key in store, store.get(key))
    cache.set = lambda key, value: store.__setitem__(key, value)
    client, _, _ = make_client(*[FakeResponse(503, {}) for _ in range(3)], cache=cache)

    with pytest.raises(MusicBrainzError):
        client.lookup_artist("Green Day", "abc-123")
    assert store == {}


def test_connection_error_is_wrapped():
    client, _, _ = make_client(requests.ConnectionError("boom"), max_retries=0)

    with pytest.raises(MusicBrainzError):
        client.lookup_artist("Green Day", "abc-123")


def test_calls_are_spaced_out():
    client, _, fake_time = make_client(FakeResponse(200, ARTIST), FakeResponse(200, ARTIST), min_interval=1.1)

    client.lookup_artist("A", "1")
    client.lookup_artist("B", "2")

    assert fake_time.sleeps == [pytest.approx(1.1)]


def test_blank_user_agent_is_rejected():
    with pytest.raises(ValueError):
        MusicBrainzClient("  ")


def test_disk_cache_roundtrip_expiry_and_corrupt_file(tmp_path):
    now = [1000.0]
    path = tmp_path / "sub" / "cache.json"
    cache = DiskCache(path, ttl_seconds=100, now=lambda: now[0])

    assert cache.get("k") == (False, None)
    cache.set("k", {"a": 1})
    cache.set("none", None)

    reloaded = DiskCache(path, ttl_seconds=100, now=lambda: now[0])
    assert reloaded.get("k") == (True, {"a": 1})
    assert reloaded.get("none") == (True, None)
    now[0] += 101
    assert reloaded.get("k") == (False, None)

    path.write_text("{깨진 json", encoding="utf-8")
    assert DiskCache(path, ttl_seconds=100).get("k") == (False, None)
