import io

import pytest
from PIL import Image

from taste_critic import config
from taste_critic.cache import DiskCache
from taste_critic.share_card import CardFontError, find_font_path, render_share_card, wrap_text
from taste_critic.usage import UsageLimiter, UsageLimitError


class FixedFont:
    """글자당 10px로 가정하는 가짜 글꼴."""

    def getlength(self, text):
        return 10 * len(text)


def test_wrap_text_breaks_by_characters():
    assert wrap_text("가나다라마바사", FixedFont(), 30, 5) == ["가나다", "라마바", "사"]


def test_wrap_text_prefers_word_boundaries():
    assert wrap_text("가나다 라마바 사", FixedFont(), 60, 5) == ["가나다", "라마바 사"]
    assert wrap_text("가나 다라 마바", FixedFont(), 50, 5) == ["가나 다라", "마바"]
    assert wrap_text("가나다 라마바", FixedFont(), 30, 5) == ["가나다", "라마바"]


def test_wrap_text_splits_only_words_longer_than_a_line():
    assert wrap_text("가 나다라마바사", FixedFont(), 30, 5) == ["가", "나다라", "마바사"]


def test_wrap_text_ellipsizes_when_too_long():
    lines = wrap_text("가나다라마바사아자차", FixedFont(), 30, 2)

    assert lines == ["가나다", "라마…"]
    assert all(FixedFont().getlength(line) <= 30 for line in lines)


def test_wrap_text_handles_newlines_and_empty():
    assert wrap_text("가\n나", FixedFont(), 100, 3) == ["가 나"]
    assert wrap_text("   ", FixedFont(), 100, 3) == []


def has_font():
    try:
        find_font_path()
        find_font_path(bold=True)
    except CardFontError:
        return False
    return True


@pytest.mark.skipif(not has_font(), reason="한글 글꼴이 없는 환경")
def test_render_share_card_returns_png_of_expected_size():
    data = render_share_card("펑크 록에 깊이 빠진 한 달, 그리고 새벽의 귀" * 3, 7, 10, ["Green Day", "A" * 80], "최근 3개월")

    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG" and image.size == (1080, 1350)


@pytest.mark.skipif(not has_font(), reason="한글 글꼴이 없는 환경")
def test_render_share_card_without_artists():
    assert render_share_card("총평", 1, 10, [], "전체 기간")[:8] == b"\x89PNG\r\n\x1a\n"


def test_missing_font_raises_clear_error(monkeypatch):
    import taste_critic.share_card as card

    monkeypatch.setattr(card, "_REGULAR_FONTS", ())
    monkeypatch.delenv("TASTE_CRITIC_FONT", raising=False)

    with pytest.raises(CardFontError):
        card.find_font_path()


def test_font_env_override(monkeypatch, tmp_path):
    font = tmp_path / "f.ttf"
    font.write_bytes(b"x")
    monkeypatch.setenv("TASTE_CRITIC_FONT", str(font))

    assert find_font_path() == str(font)
    assert find_font_path(bold=True) == str(font)  # 굵은 글꼴 지정이 없으면 같은 글꼴을 쓴다


def test_usage_limiter_blocks_then_recovers():
    now = [0.0]
    limiter = UsageLimiter(2, window_seconds=3600, now=lambda: now[0])

    limiter.consume()
    now[0] += 10
    limiter.consume()
    with pytest.raises(UsageLimitError) as info:
        limiter.consume()
    assert "2번" in str(info.value) and "60분" in str(info.value)

    now[0] += 3590  # 첫 호출이 창에서 빠진다
    limiter.consume()


def test_usage_limiter_zero_means_unlimited():
    limiter = UsageLimiter(0)

    for _ in range(100):
        limiter.consume()


def test_disk_cache_prunes_expired_and_oldest_on_load(tmp_path):
    path = tmp_path / "c.json"
    now = [1000.0]
    writer = DiskCache(path, ttl_seconds=100, now=lambda: now[0])
    writer.set("old", 1)
    now[0] = 1050.0
    for i in range(3):
        now[0] += 1
        writer.set(f"k{i}", i)

    now[0] = 1120.0  # old(1000)는 만료, 나머지는 유효
    reader = DiskCache(path, ttl_seconds=100, now=lambda: now[0], max_entries=2)

    assert reader.get("old") == (False, None)
    assert reader.get("k0") == (False, None)  # 개수 제한으로 가장 오래된 것부터 제거
    assert reader.get("k1") == (True, 1) and reader.get("k2") == (True, 2)


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.setenv("LASTFM_API_KEY", "key")
    monkeypatch.delenv("AI_CALLS_PER_HOUR", raising=False)


def test_ai_calls_per_hour_setting(monkeypatch, clean_env):
    assert config.load_settings().ai_calls_per_hour == config.DEFAULT_AI_CALLS_PER_HOUR
    monkeypatch.setenv("AI_CALLS_PER_HOUR", "5")
    assert config.load_settings().ai_calls_per_hour == 5
    monkeypatch.setenv("AI_CALLS_PER_HOUR", "0")
    assert config.load_settings().ai_calls_per_hour == 0


@pytest.mark.parametrize("value", ["abc", "-1", "1.5"])
def test_ai_calls_per_hour_rejects_bad_values(monkeypatch, clean_env, value):
    monkeypatch.setenv("AI_CALLS_PER_HOUR", value)

    with pytest.raises(config.ConfigError):
        config.load_settings()
