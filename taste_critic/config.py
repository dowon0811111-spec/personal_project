"""환경 변수 설정.

Claude 연결 정보(ANTHROPIC_*)는 ai_helper.py가 직접 읽으므로 여기서 다루지 않는다.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_MUSICBRAINZ_USER_AGENT = "taste-critic/0.1 (contact@example.com)"
DEFAULT_AI_CALLS_PER_HOUR = 20


class ConfigError(RuntimeError):
    """필수 환경 변수가 없을 때 발생한다."""


@dataclass(frozen=True)
class Settings:
    lastfm_api_key: str
    musicbrainz_user_agent: str
    ai_calls_per_hour: int = DEFAULT_AI_CALLS_PER_HOUR  # 0이면 제한 없음


def load_settings() -> Settings:
    load_dotenv()
    lastfm_api_key = os.getenv("LASTFM_API_KEY", "").strip()
    if not lastfm_api_key:
        raise ConfigError(".env에 LASTFM_API_KEY가 없습니다. .env.example을 참고해 추가하세요.")
    user_agent = os.getenv("MUSICBRAINZ_USER_AGENT", "").strip() or DEFAULT_MUSICBRAINZ_USER_AGENT
    raw_limit = os.getenv("AI_CALLS_PER_HOUR", "").strip()
    if raw_limit and not (raw_limit.isdigit()):
        raise ConfigError("AI_CALLS_PER_HOUR는 0 이상의 정수여야 합니다. (0이면 제한 없음)")
    return Settings(
        lastfm_api_key=lastfm_api_key,
        musicbrainz_user_agent=user_agent,
        ai_calls_per_hour=int(raw_limit) if raw_limit else DEFAULT_AI_CALLS_PER_HOUR,
    )
