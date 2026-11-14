import pytest

from taste_critic import config


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    # 실제 .env 값이 테스트에 섞이지 않게 한다.
    monkeypatch.setattr(config, "load_dotenv", lambda: None)


def test_missing_lastfm_key_raises(monkeypatch):
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)

    with pytest.raises(config.ConfigError):
        config.load_settings()


def test_loads_values_and_defaults_user_agent(monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", " key ")
    monkeypatch.delenv("MUSICBRAINZ_USER_AGENT", raising=False)

    settings = config.load_settings()

    assert settings.lastfm_api_key == "key"
    assert settings.musicbrainz_user_agent == config.DEFAULT_MUSICBRAINZ_USER_AGENT
