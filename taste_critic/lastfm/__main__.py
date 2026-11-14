"""실제 Last.fm API로 연결을 확인하는 스크립트.

사용법: py -m taste_critic.lastfm <Last.fm 사용자명>
"""

import sys

from taste_critic.config import ConfigError, load_settings

from .client import LastFmClient, LastFmError
from .models import validate_username


def main() -> int:
    if len(sys.argv) != 2:
        print("사용법: py -m taste_critic.lastfm <Last.fm 사용자명>")
        return 2
    try:
        user = validate_username(sys.argv[1])
        client = LastFmClient(load_settings().lastfm_api_key)
        artists = client.get_top_artists(user, period="1month", limit=5)
        tags = client.get_top_tags(user, limit=5)
        recent = client.get_recent_tracks(user, limit=5)
    except (ConfigError, ValueError, LastFmError) as exc:
        print(f"오류: {exc}")
        return 1

    print(f"[{user}] 최근 1개월 상위 아티스트")
    for artist in artists:
        print(f"  {artist.rank}. {artist.name} ({artist.playcount}회)")
    print("상위 태그: " + ", ".join(tag.name for tag in tags))
    print(f"전체 청취 기록: {recent.total}곡")
    for track in recent.tracks:
        print(f"  {track.played_at:%Y-%m-%d %H:%M} UTC  {track.artist} - {track.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
