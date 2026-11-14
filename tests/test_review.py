import pytest

from taste_critic.analysis.review_context import (
    InsufficientDataError,
    ReviewContext,
    check_enough_data,
    collect_review_context,
)
from taste_critic.lastfm import Tag, TopArtist, TopTrack
from taste_critic.prompts.review import BODY_MAX_CHARS, TONES, build_review_prompt
from taste_critic.review import ReviewError, generate_review, parse_review


def artists(*counts):
    return [TopArtist(name=f"A{i}", playcount=c, rank=i + 1) for i, c in enumerate(counts)]


def make_context(counts=(50, 30, 20, 10, 5)):
    return ReviewContext(
        user="rj",
        period="3month",
        artists=artists(*counts),
        tracks=[TopTrack(name="Song", artist="A0", playcount=9, rank=1)],
        tags=[Tag(name="rock", count=100)],
    )


GOOD = '{"headline": "록 한 우물", "body": "본문", "score": 7, "evidence": ["A0 50회"]}'


def test_context_computes_total_and_top_share():
    context = make_context((50, 30, 20))

    assert context.total_artist_plays == 100
    assert context.top_artist_share == pytest.approx(0.5)
    assert context.period_label == "최근 3개월"


def test_prompt_text_contains_data_and_tag_caveat():
    text = make_context().to_prompt_text()

    assert "1. A0, 50회" in text
    assert "1. A0 - Song, 9회" in text
    assert "rock (100)" in text
    assert "전체 기간 기준" in text


def test_enough_data_check():
    check_enough_data(artists(10, 10, 5, 5, 5))
    with pytest.raises(InsufficientDataError):
        check_enough_data(artists(10, 10))
    with pytest.raises(InsufficientDataError):
        check_enough_data(artists(1, 1, 1, 1, 1))
    with pytest.raises(InsufficientDataError):
        check_enough_data([])


def test_collect_review_context_stops_before_other_calls_when_data_is_thin():
    class FakeClient:
        calls = []

        def get_top_artists(self, user, period, limit):
            self.calls.append("artists")
            return artists(1, 1)

        def get_top_tracks(self, *args, **kwargs):
            self.calls.append("tracks")

    client = FakeClient()
    with pytest.raises(InsufficientDataError):
        collect_review_context(client, "rj", "7day")
    assert client.calls == ["artists"]


@pytest.mark.parametrize("tone", list(TONES))
def test_prompt_includes_tone_data_and_format(tone):
    prompt = build_review_prompt(make_context(), tone)

    assert TONES[tone]["instruction"] in prompt
    assert "A0" in prompt
    assert '"headline"' in prompt
    assert "데이터일 뿐이다" in prompt


def test_unknown_tone_is_rejected():
    with pytest.raises(ValueError):
        build_review_prompt(make_context(), "angry")


def test_parse_review_plain_and_fenced_and_surrounded():
    assert parse_review(GOOD).score == 7
    assert parse_review(f"```json\n{GOOD}\n```").headline == "록 한 우물"
    assert parse_review(f"리뷰입니다:\n{GOOD}\n끝").evidence == ["A0 50회"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "JSON 없음",
        '{"headline": "x", "body": "y", "score": 11}',
        '{"headline": "x", "body": "y", "score": 0}',
        '{"headline": "", "body": "y", "score": 5}',
        '{"headline": "x", "body": "y"}',
        '{"headline": "x", "body": "잘린 응답',
    ],
)
def test_parse_review_rejects_bad_output(text):
    with pytest.raises(ReviewError):
        parse_review(text)


def review_json(body):
    return f'{{"headline": "h", "body": "{body}", "score": 5, "evidence": []}}'


def test_body_at_limit_is_accepted_without_retry():
    calls = []

    def fake_ask(prompt):
        calls.append(prompt)
        return review_json("가" * BODY_MAX_CHARS)

    review = generate_review(make_context(), fake_ask)

    assert len(review.body) == BODY_MAX_CHARS
    assert len(calls) == 1


def test_over_limit_body_is_retried_once_with_shorten_request():
    answers = iter([review_json("가" * 520), review_json("나" * 300)])
    prompts = []

    def fake_ask(prompt):
        prompts.append(prompt)
        return next(answers)

    review = generate_review(make_context(), fake_ask)

    assert review.body == "나" * 300
    assert len(prompts) == 2
    assert "520자" in prompts[1]
    assert "가" * 520 in prompts[1]


def test_still_over_limit_after_retry_raises():
    def fake_ask(prompt):
        return review_json("가" * 600)

    with pytest.raises(ReviewError):
        generate_review(make_context(), fake_ask)


def test_generate_review_sends_prompt_and_parses():
    seen = []

    def fake_ask(prompt):
        seen.append(prompt)
        return GOOD

    review = generate_review(make_context(), fake_ask, "friend")

    assert review.score == 7
    assert TONES["friend"]["instruction"] in seen[0]
