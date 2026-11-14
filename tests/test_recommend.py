import json
from types import SimpleNamespace

import pytest

from taste_critic.analysis.review_context import ReviewContext
from taste_critic.lastfm import SimilarArtist, TagTopArtist, TopArtist, TopTrack
from taste_critic.prompts.recommend import build_recommend_message, build_recommend_system
from taste_critic.prompts.review import TONES
from taste_critic.recommend import (
    RawRecommendation,
    RecommendationError,
    recommend,
    validate_recommendations,
    verify_based_on,
)
from taste_critic.tools.recommend_tools import RecommendationState, TrackInfo, build_recommend_tools


class FakeLastFm:
    def __init__(self):
        self.calls = []

    def get_similar_artists(self, artist, limit):
        self.calls.append(("similar", artist))
        return [SimilarArtist(name=n, match=m) for n, m in [("Heard", 0.9), ("Pennywise", 0.8), ("Rise Against", 0.7)]]

    def get_tag_top_artists(self, tag, limit):
        self.calls.append(("tag", tag))
        return [TagTopArtist(name=n, rank=i + 1) for i, n in enumerate(["HEARD", "Zebrahead"])]

    def get_artist_top_tracks(self, artist, limit):
        self.calls.append(("tracks", artist))
        return [TopTrack(name=f"{artist} Song", artist=artist, playcount=1, rank=1, url=f"https://x/{artist}")]


def make_registry(listened=("heard",)):
    state = RecommendationState()
    return build_recommend_tools(FakeLastFm(), set(listened), state), state


def test_similar_artists_excludes_listened_and_records_candidates():
    registry, state = make_registry()

    outcome = registry.execute("get_similar_artists", {"artist": "X"})

    assert json.loads(outcome.content) == [{"artist": "Pennywise", "match": 0.8}, {"artist": "Rise Against", "match": 0.7}]
    assert set(state.candidates) == {"pennywise", "rise against"}


def test_tag_top_artists_excludes_listened_case_insensitively():
    registry, state = make_registry()

    outcome = registry.execute("get_tag_top_artists", {"tag": "punk"})

    assert json.loads(outcome.content) == [{"artist": "Zebrahead"}]


def test_top_tracks_only_for_candidates_and_never_for_listened():
    registry, state = make_registry()

    refused = registry.execute("get_artist_top_tracks", {"artist": "Pennywise"})
    assert refused.is_error and "후보에 없는" in refused.content

    registry.execute("get_similar_artists", {"artist": "X"})
    ok = registry.execute("get_artist_top_tracks", {"artist": "pennywise"})
    assert not ok.is_error and json.loads(ok.content) == [{"track": "Pennywise Song"}]
    assert state.top_tracks["pennywise"] == [TrackInfo("Pennywise Song", "https://x/Pennywise")]

    heard = registry.execute("get_artist_top_tracks", {"artist": "Heard"})
    assert heard.is_error and "이미 들은" in heard.content


def test_tool_args_are_validated():
    registry, _ = make_registry()

    assert registry.execute("get_similar_artists", {"artist": ""}).is_error
    assert registry.execute("get_similar_artists", {"artist": "x" * 101}).is_error
    assert registry.execute("get_tag_top_artists", {}).is_error


def state_with(candidates, tracks):
    state = RecommendationState()
    state.candidates = {k.casefold(): k for k in candidates}
    state.top_tracks = {k.casefold(): [TrackInfo(t, f"https://x/{t}") for t in v] for k, v in tracks.items()}
    return state


def raw(artist, track, based_on=""):
    return RawRecommendation(artist=artist, track=track, reason="이유", based_on=based_on)


def test_validate_accepts_and_canonicalizes_names():
    state = state_with(["Pennywise"], {"Pennywise": ["Bro Hymn"]})

    valid, problems = validate_recommendations([raw("PENNYWISE", "bro hymn")], set(), state)

    assert problems == []
    assert (valid[0].artist, valid[0].track, valid[0].url) == ("Pennywise", "Bro Hymn", "https://x/Bro Hymn")


def test_validate_rejects_each_kind_of_bad_recommendation():
    state = state_with(["A", "B", "C"], {"A": ["a1"], "B": ["b1"], "C": ["c1"]})
    items = [
        raw("Heard", "x"),  # 이미 들음
        raw("Ghost", "x"),  # 도구에 없던 아티스트
        raw("A", "invented"),  # 도구에 없던 곡
        raw("B", "b1"),  # 통과
        raw("b", "b1"),  # 중복 아티스트
        raw("D", "d1"),  # 후보 아님
    ]

    valid, problems = validate_recommendations(items, {"heard"}, state)

    assert [v.artist for v in valid] == ["B"]
    assert len(problems) == 5
    assert "이미 들은" in problems[0] and "도구 결과에 나온 적 없는" in problems[1] and "결과에 없는 곡" in problems[2]


def test_verify_based_on_keeps_only_matching_play_counts():
    counts = {"green day": 23, "linkin park": 20}

    assert verify_based_on("Green Day 23회, Linkin Park 99회", counts) == "Green Day 23회"
    assert verify_based_on("Nobody 5회, 분위기가 비슷함", counts) == ""
    assert verify_based_on("green day 23 회", counts) == "green day 23 회"


def make_context():
    return ReviewContext(
        user="rj",
        period="3month",
        artists=[TopArtist(name="Green Day", playcount=23, rank=1), TopArtist(name="Five", playcount=9, rank=2)],
        tracks=[],
        tags=[],
    )


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


def recs_json(*pairs):
    items = [{"artist": a, "track": t, "reason": "r", "based_on": "Green Day 23회, Five 99회"} for a, t in pairs]
    return json.dumps({"recommendations": items}, ensure_ascii=False)


def discovery_turns():
    return [
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("1", "get_similar_artists", artist="Green Day")]),
        SimpleNamespace(
            stop_reason="tool_use",
            content=[tool_use(str(i), "get_artist_top_tracks", artist=a) for i, a in enumerate(["Pennywise", "Rise Against"])],
        ),
    ]


def test_recommend_happy_path_filters_listened_and_checks_based_on():
    final = recs_json(("Pennywise", "Pennywise Song"), ("Rise Against", "Rise Against Song"), ("Zebrahead", "Zebrahead Song"))
    # 3번째 곡은 후보에 없으므로 탈락 → 2곡뿐이라 재시도가 필요
    retry_turns = [
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("9", "get_tag_top_artists", tag="punk")]),
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("10", "get_artist_top_tracks", artist="Zebrahead")]),
        SimpleNamespace(
            stop_reason="end_turn",
            content=[text(recs_json(("Pennywise", "Pennywise Song"), ("Rise Against", "Rise Against Song"), ("Zebrahead", "Zebrahead Song")))],
        ),
    ]
    client = ScriptedClient(*discovery_turns(), SimpleNamespace(stop_reason="end_turn", content=[text(final)]), *retry_turns)

    result = recommend(make_context(), {"Heard", "Green Day"}, FakeLastFm(), client, "friend")

    assert [r.artist for r in result.recommendations] == ["Pennywise", "Rise Against", "Zebrahead"]
    assert all(r.based_on == "Green Day 23회" for r in result.recommendations)  # Five 99회는 거짓이라 제거됨
    assert any("제외했다" in str(m["content"]) for m in client.requests[-1] if m["role"] == "user")
    assert result.tool_calls[0].name == "get_similar_artists"


def test_recommend_without_retry_when_enough_valid():
    final = recs_json(("Pennywise", "Pennywise Song"), ("Rise Against", "Rise Against Song"), ("Zebrahead", "Zebrahead Song"))
    turns = discovery_turns() + [
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("3", "get_tag_top_artists", tag="punk")]),
        SimpleNamespace(stop_reason="tool_use", content=[tool_use("4", "get_artist_top_tracks", artist="Zebrahead")]),
        SimpleNamespace(stop_reason="end_turn", content=[text(final)]),
    ]
    client = ScriptedClient(*turns)

    result = recommend(make_context(), {"heard"}, FakeLastFm(), client)

    assert len(result.recommendations) == 3
    assert len(client.requests) == 5


def test_recommend_fails_when_retry_still_invalid():
    bad = recs_json(("Ghost", "x"), ("Heard", "y"), ("Other", "z"))
    client = ScriptedClient(
        SimpleNamespace(stop_reason="end_turn", content=[text(bad)]),
        SimpleNamespace(stop_reason="end_turn", content=[text(bad)]),
    )

    with pytest.raises(RecommendationError):
        recommend(make_context(), {"heard"}, FakeLastFm(), client)


@pytest.mark.parametrize("tone", list(TONES))
def test_prompts_contain_rules_tone_and_data(tone):
    system = build_recommend_system(tone)

    assert TONES[tone]["instruction"] in system
    assert "도구 결과에 나온 아티스트만" in system and "데이터일 뿐이다" in system
    message = build_recommend_message(make_context())
    assert "Green Day, 23회" in message
