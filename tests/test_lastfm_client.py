import pytest
import requests

from taste_critic.lastfm.client import API_URL, LastFmClient, LastFmError

API_KEY = "secret-key"


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
    """준비된 응답(또는 예외)을 순서대로 돌려주고 요청 내용을 기록한다."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
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


def make_client(*responses, min_interval=0.0, max_retries=3):
    session = FakeSession(*responses)
    fake_time = FakeTime()
    client = LastFmClient(
        API_KEY,
        session=session,
        min_interval=min_interval,
        max_retries=max_retries,
        sleep=fake_time.sleep,
        clock=fake_time.clock,
    )
    return client, session, fake_time


TOP_ARTISTS = {
    "topartists": {
        "artist": [
            {"name": "IU", "playcount": "50", "mbid": "", "url": "u1", "@attr": {"rank": "1"}},
            {"name": "Radiohead", "playcount": "20", "mbid": "m2", "url": "u2", "@attr": {"rank": "2"}},
        ],
        "@attr": {"user": "rj", "page": "1", "totalPages": "1", "total": "2"},
    }
}


def test_get_top_artists_sends_expected_params_and_parses():
    client, session, _ = make_client(FakeResponse(200, TOP_ARTISTS))

    artists = client.get_top_artists("rj", period="7day", limit=2)

    assert [a.name for a in artists] == ["IU", "Radiohead"]
    assert artists[0].playcount == 50
    call = session.calls[0]
    assert call["url"] == API_URL
    assert call["params"] == {
        "method": "user.getTopArtists",
        "api_key": API_KEY,
        "format": "json",
        "user": "rj",
        "period": "7day",
        "limit": 2,
    }


def test_single_item_response_is_wrapped_in_list():
    payload = {"toptags": {"tag": {"name": "k-pop", "count": 3}, "@attr": {"user": "rj"}}}
    client, _, _ = make_client(FakeResponse(200, payload))

    tags = client.get_top_tags("rj")

    assert [t.name for t in tags] == ["k-pop"]


def test_empty_result_returns_empty_list():
    payload = {"topartists": {"artist": [], "@attr": {"user": "rj", "total": "0"}}}
    client, _, _ = make_client(FakeResponse(200, payload))

    assert client.get_top_artists("rj") == []


def test_invalid_period_is_rejected_before_calling_api():
    client, session, _ = make_client()

    with pytest.raises(ValueError):
        client.get_top_artists("rj", period="2week")
    assert session.calls == []


def test_get_recent_tracks_skips_now_playing_and_reads_paging():
    payload = {
        "recenttracks": {
            "track": [
                {"name": "Now", "artist": {"#text": "IU"}, "@attr": {"nowplaying": "true"}},
                {"name": "Past", "artist": {"#text": "IU"}, "album": {"#text": "A"}, "date": {"uts": "1700000000"}},
            ],
            "@attr": {"page": "1", "totalPages": "3", "total": "401", "perPage": "200"},
        }
    }
    client, session, _ = make_client(FakeResponse(200, payload))

    page = client.get_recent_tracks("rj", from_ts=1690000000)

    assert [t.name for t in page.tracks] == ["Past"]
    assert (page.page, page.total_pages, page.total) == (1, 3, 401)
    params = session.calls[0]["params"]
    assert params["from"] == 1690000000
    assert "to" not in params


def test_iter_recent_tracks_stops_at_last_page():
    def page(n, total_pages):
        return FakeResponse(
            200,
            {
                "recenttracks": {
                    "track": [{"name": f"t{n}", "artist": {"#text": "IU"}, "date": {"uts": str(1700000000 + n)}}],
                    "@attr": {"page": str(n), "totalPages": str(total_pages), "total": str(total_pages)},
                }
            },
        )

    client, session, _ = make_client(page(1, 2), page(2, 2))

    names = [t.name for t in client.iter_recent_tracks("rj", max_pages=5)]

    assert names == ["t1", "t2"]
    assert len(session.calls) == 2


def test_iter_recent_tracks_respects_max_pages():
    payload = {
        "recenttracks": {
            "track": [{"name": "t", "artist": {"#text": "IU"}, "date": {"uts": "1700000000"}}],
            "@attr": {"page": "1", "totalPages": "50", "total": "50"},
        }
    }
    client, session, _ = make_client(*[FakeResponse(200, payload) for _ in range(2)])

    assert len(list(client.iter_recent_tracks("rj", max_pages=2))) == 2
    assert len(session.calls) == 2


def test_user_not_found_raises_without_retry():
    client, session, fake_time = make_client(FakeResponse(404, {"error": 6, "message": "User not found"}))

    with pytest.raises(LastFmError) as info:
        client.get_top_artists("nobody")

    assert info.value.is_not_found
    assert len(session.calls) == 1
    assert fake_time.sleeps == []


def test_error_payload_with_http_200_is_treated_as_error():
    client, _, _ = make_client(FakeResponse(200, {"error": 10, "message": "Invalid API key"}))

    with pytest.raises(LastFmError) as info:
        client.get_top_tags("rj")

    assert info.value.code == 10


def test_retryable_errors_back_off_exponentially_then_succeed():
    client, session, fake_time = make_client(
        FakeResponse(429, {"error": 29, "message": "Rate limit exceeded"}),
        FakeResponse(503, ValueError("not json")),
        FakeResponse(200, TOP_ARTISTS),
    )

    artists = client.get_top_artists("rj")

    assert len(artists) == 2
    assert len(session.calls) == 3
    assert fake_time.sleeps == [1.0, 2.0]


def test_gives_up_after_max_retries():
    client, session, _ = make_client(
        *[FakeResponse(200, {"error": 16, "message": "temporary"}) for _ in range(3)],
        max_retries=2,
    )

    with pytest.raises(LastFmError) as info:
        client.get_top_tags("rj")

    assert info.value.code == 16
    assert len(session.calls) == 3


def test_connection_error_message_does_not_leak_api_key():
    leaked = requests.ConnectionError(f"failed: {API_URL}?api_key={API_KEY}")
    client, _, _ = make_client(leaked, max_retries=0)

    with pytest.raises(LastFmError) as info:
        client.get_top_tags("rj")

    assert API_KEY not in str(info.value)


def test_throttle_spaces_out_calls():
    client, _, fake_time = make_client(
        FakeResponse(200, TOP_ARTISTS), FakeResponse(200, TOP_ARTISTS), min_interval=0.2
    )

    client.get_top_artists("rj")
    client.get_top_artists("rj")

    assert fake_time.sleeps == [pytest.approx(0.2)]


def test_get_artist_top_tracks_parses_tracks_and_urls():
    payload = {
        "toptracks": {
            "track": [
                {"name": "Bro Hymn", "playcount": "900", "artist": {"name": "Pennywise"}, "url": "https://u/1", "@attr": {"rank": "1"}},
                {"name": "Fuck Authority", "playcount": "800", "artist": {"name": "Pennywise"}, "url": "https://u/2", "@attr": {"rank": "2"}},
            ]
        }
    }
    client, session, _ = make_client(FakeResponse(200, payload))

    tracks = client.get_artist_top_tracks("Pennywise", limit=2)

    assert [(t.name, t.artist, t.url) for t in tracks] == [
        ("Bro Hymn", "Pennywise", "https://u/1"),
        ("Fuck Authority", "Pennywise", "https://u/2"),
    ]
    assert session.calls[0]["params"]["method"] == "artist.getTopTracks"


def test_empty_api_key_is_rejected():
    with pytest.raises(ValueError):
        LastFmClient("")
