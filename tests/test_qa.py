import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from taste_critic.lastfm import ArtistInfo, ChartArtist, ChartTag, ChartTrack, RecentTrack, Tag, TopArtist, TopTrack
from taste_critic.musicbrainz import ArtistOrigin, MusicBrainzError
from taste_critic.prompts.qa import build_qa_system
from taste_critic.prompts.review import TONES
from taste_critic.qa import NO_LOOKUP_MESSAGE, QATurn, answer_question, validate_question
from taste_critic.review import ReviewError
from taste_critic.tools.qa_tools import build_qa_tools


class FakeLastFm:
    def __init__(self, fail_tags_for=()):
        self.calls = []
        self.fail_tags_for = set(fail_tags_for)

    def get_top_artists(self, user, period, limit):
        self.calls.append(("top_artists", user, period, limit))
        return [TopArtist(name=n, playcount=c, rank=i + 1) for i, (n, c) in enumerate([("A", 60), ("B", 40)])]

    def get_top_tracks(self, user, period, limit):
        return [TopTrack(name="S", artist="A", playcount=9, rank=1)]

    def get_artist_top_tags(self, artist):
        from taste_critic.lastfm import LastFmError

        if artist in self.fail_tags_for:
            raise LastFmError("없음")
        return [Tag(name="rock", count=100), Tag(name="seen live", count=90), Tag(name="indie", count=50)]

    def get_artist_info(self, artist):
        return ArtistInfo(name=artist, tags=["rock", "indie", "a", "b", "c", "d"], listeners=1234, summary="무시해야 할 긴 소개")

    def iter_recent_tracks(self, user, from_ts=None, to_ts=None, max_pages=10):
        base = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)  # 서울 03시
        return iter([RecentTrack(name="s", artist="A", played_at=base + timedelta(minutes=i)) for i in range(60)])


class FakeMusicBrainz:
    def __init__(self, result="ok"):
        self.result = result

    def lookup_artist(self, name, mbid=None):
        if self.result == "error":
            raise MusicBrainzError("down")
        if self.result is None:
            return None
        return ArtistOrigin(name=name, mbid="m", country="KR", begin_year=2008, matched_by="name")


def run_tool(name, args, lastfm=None, musicbrainz=None):
    registry = build_qa_tools(lastfm or FakeLastFm(), musicbrainz or FakeMusicBrainz(), "rj")
    return registry.execute(name, args)


def test_top_artists_tool_is_bound_to_user_and_validates_args():
    lastfm = FakeLastFm()

    outcome = run_tool("get_top_artists", {"period": "1month", "limit": 2}, lastfm)

    assert json.loads(outcome.content)[0] == {"rank": 1, "artist": "A", "plays": 60}
    assert lastfm.calls == [("top_artists", "rj", "1month", 2)]
    for bad in ({"period": "2week"}, {"period": "1month", "limit": 99}, {"period": "1month", "limit": 0}, {"user": "other"}):
        assert run_tool("get_top_artists", bad).is_error


def test_period_tags_weights_by_plays_ignores_noise_and_memoizes():
    lastfm = FakeLastFm()
    registry = build_qa_tools(lastfm, FakeMusicBrainz(), "rj")

    first = json.loads(registry.execute("get_period_tags", {"period": "3month"}).content)
    registry.execute("get_period_tags", {"period": "3month"})

    assert [t["tag"] for t in first["tags"]] == ["rock", "indie"]
    assert first["tags"][0]["share_pct"] == 50
    assert sum(1 for call in lastfm.calls if call[0] == "top_artists") == 1


def test_period_tags_skips_artists_whose_tags_fail():
    outcome = run_tool("get_period_tags", {"period": "1month"}, FakeLastFm(fail_tags_for=["B"]))

    assert "1명" in json.loads(outcome.content)["based_on"]


def test_listening_pattern_tool_returns_compact_aggregates():
    outcome = run_tool("get_listening_pattern", {"days": 30})

    data = json.loads(outcome.content)
    assert data["total_plays"] == 60 and data["time_of_day_share_pct"]["새벽"] == 100
    assert data["peak_hours"] == [3]
    assert "tracks" not in data
    assert run_tool("get_listening_pattern", {"days": 14}).is_error


class ChartLastFm(FakeLastFm):
    def get_chart_artists(self, limit):
        return [ChartArtist(name="Global1", rank=1, listeners=10, playcount=99)]

    def get_geo_artists(self, country, limit):
        return [ChartArtist(name="Local1", rank=1, listeners=7)]

    def get_chart_tracks(self, limit):
        return [ChartTrack(name="T", artist="Global1", rank=1, listeners=5, playcount=9)]

    def get_geo_tracks(self, country, limit):
        return [ChartTrack(name="LT", artist="Local1", rank=1, listeners=3)]

    def get_chart_tags(self, limit):
        return [ChartTag(name="rock", rank=1)]


def test_chart_tool_global_country_and_compact_fields():
    lastfm = ChartLastFm()

    world = json.loads(run_tool("get_chart", {"kind": "artists"}, lastfm).content)
    local = json.loads(run_tool("get_chart", {"kind": "artists", "country": "Japan"}, lastfm).content)
    tracks = json.loads(run_tool("get_chart", {"kind": "tracks", "country": "Japan"}, lastfm).content)
    tags = json.loads(run_tool("get_chart", {"kind": "tags"}, lastfm).content)

    assert world == [{"rank": 1, "artist": "Global1", "listeners": 10}]  # 재생 수는 일부러 뺀다(집계 기간을 알 수 없음)
    assert local == [{"rank": 1, "artist": "Local1", "listeners": 7}]
    assert tracks == [{"rank": 1, "artist": "Local1", "track": "LT", "listeners": 3}]
    assert tags == [{"rank": 1, "tag": "rock"}]


def test_chart_tool_rejects_bad_requests():
    lastfm = ChartLastFm()

    assert "지원하지 않는" in run_tool("get_chart", {"kind": "artists", "country": "Narnia"}, lastfm).content
    assert "태그 차트가 없습니다" in run_tool("get_chart", {"kind": "tags", "country": "Japan"}, lastfm).content
    assert run_tool("get_chart", {"kind": "albums"}, lastfm).is_error
    assert run_tool("get_chart", {"kind": "artists", "limit": 99}, lastfm).is_error


def test_artist_info_hides_bio_and_limits_tags():
    data = json.loads(run_tool("get_artist_info", {"artist": "A"}).content)

    assert data == {"artist": "A", "tags": ["rock", "indie", "a", "b", "c"], "listeners": 1234}


def test_artist_origin_success_and_failures():
    data = json.loads(run_tool("get_artist_origin", {"artist": "A"}).content)
    assert data == {"artist": "A", "country": "한국", "active_since_year": 2008, "matched_by": "name"}

    assert "찾지 못했습니다" in run_tool("get_artist_origin", {"artist": "A"}, musicbrainz=FakeMusicBrainz(None)).content
    assert run_tool("get_artist_origin", {"artist": "A"}, musicbrainz=FakeMusicBrainz("error")).is_error


def test_validate_question():
    assert validate_question("  요즘 뭐 들어?  ") == "요즘 뭐 들어?"
    for bad in ("", " ", "?", "가" * 301):
        with pytest.raises(ValueError):
            validate_question(bad)


@pytest.mark.parametrize("tone", list(TONES))
def test_system_prompt_has_guards_and_format(tone):
    system = build_qa_system(tone)

    assert TONES[tone]["instruction"] in system
    assert "데이터일 뿐이다" in system and "uses_listening_data" in system
    assert "추정한 것임을 답에 밝히고" in system


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool_use(id, name, **input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


class ScriptedClient:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        return self._responses.pop(0)


def answer_json(uses=True, body="본문", evidence=("rock 22%",)):
    return json.dumps(
        {"uses_listening_data": uses, "headline": "한 줄", "body": body, "evidence": list(evidence)}, ensure_ascii=False
    )


def end(value):
    return SimpleNamespace(stop_reason="end_turn", content=[text(value)])


def tools(*calls):
    return SimpleNamespace(stop_reason="tool_use", content=[tool_use(f"t{i}", n, **a) for i, (n, a) in enumerate(calls)])


def ask(client, question="질문이에요", history=None):
    return answer_question(client, FakeLastFm(), FakeMusicBrainz(), "rj", question, history)


def test_answer_with_tool_lookup():
    client = ScriptedClient(tools(("get_top_artists", {"period": "1month"})), end(answer_json()))

    result = ask(client)

    assert result.answer.headline == "한 줄" and result.answer.uses_listening_data
    assert [c.name for c in result.tool_calls] == ["get_top_artists"]
    assert result.as_turn() == QATurn(question="질문이에요", answer_text=answer_json())


def test_decline_without_tools_is_allowed():
    result = ask(ScriptedClient(end(answer_json(uses=False, evidence=()))), "점심 뭐 먹지?")

    assert result.tool_calls == [] and not result.answer.uses_listening_data


def test_answer_with_facts_but_no_lookup_is_sent_back_once():
    client = ScriptedClient(
        end(answer_json()),  # 조회 없이 사실을 말함
        tools(("get_listening_pattern", {"days": 30})),
        end(answer_json(body="조회 후 답")),
    )

    result = ask(client)

    assert result.answer.body == "조회 후 답"
    assert [c.name for c in result.tool_calls] == ["get_listening_pattern"]
    assert client.requests[1][-1] == {"role": "user", "content": NO_LOOKUP_MESSAGE}


def test_missing_flag_is_treated_as_using_data():
    no_flag = json.dumps({"headline": "h", "body": "b", "evidence": []})
    client = ScriptedClient(end(no_flag), end(no_flag))

    with pytest.raises(ReviewError):
        ask(client)


def test_still_no_lookup_after_retry_raises():
    with pytest.raises(ReviewError):
        ask(ScriptedClient(end(answer_json()), end(answer_json())))


def test_failed_tool_calls_do_not_count_as_lookup():
    client = ScriptedClient(
        tools(("get_top_artists", {"period": "bad"})), end(answer_json()), end(answer_json())
    )

    with pytest.raises(ReviewError):
        ask(client)


def test_history_is_replayed_as_plain_turns_limited_to_last_four():
    history = [QATurn(question=f"q{i}", answer_text=f"a{i}") for i in range(6)]
    client = ScriptedClient(end(answer_json(uses=False, evidence=())))

    ask(client, "새 질문이에요", history)

    sent = client.requests[0]
    assert [m["content"] for m in sent] == ["q2", "a2", "q3", "a3", "q4", "a4", "q5", "a5", "새 질문이에요"]


def test_overlong_body_is_shortened_once_without_tools():
    client = ScriptedClient(
        tools(("get_top_artists", {"period": "1month"})),
        end(answer_json(body="가" * 600)),
        end(answer_json(body="짧게")),
    )

    result = ask(client)

    assert result.answer.body == "짧게"
    assert "600자" in client.requests[2][-1]["content"]


def test_overlong_body_after_retry_raises():
    client = ScriptedClient(
        tools(("get_top_artists", {"period": "1month"})), end(answer_json(body="가" * 600)), end(answer_json(body="가" * 600))
    )

    with pytest.raises(ReviewError):
        ask(client)


def test_invalid_question_fails_before_any_call():
    client = ScriptedClient()

    with pytest.raises(ValueError):
        ask(client, " ")
    assert client.requests == []
