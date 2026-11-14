from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from taste_critic.lastfm.models import (
    ArtistInfo,
    RecentTrack,
    SimilarArtist,
    Tag,
    TagTopArtist,
    TopArtist,
    TopTrack,
    as_list,
    validate_period,
    validate_username,
)


def test_as_list_wraps_single_object_and_none():
    assert as_list(None) == []
    assert as_list({"name": "a"}) == [{"name": "a"}]
    assert as_list([1, 2]) == [1, 2]


def test_top_artist_converts_string_numbers_and_rank():
    artist = TopArtist.model_validate(
        {"name": "Radiohead", "playcount": "123", "mbid": "", "url": "u", "@attr": {"rank": "1"}}
    )
    assert artist.playcount == 123
    assert artist.rank == 1
    assert artist.mbid is None


def test_top_track_flattens_artist_name():
    track = TopTrack.model_validate(
        {
            "name": "Creep",
            "playcount": "7",
            "mbid": "abc",
            "artist": {"name": "Radiohead", "mbid": ""},
            "@attr": {"rank": "3"},
        }
    )
    assert track.artist == "Radiohead"
    assert track.rank == 3
    assert track.mbid == "abc"


def test_tag_accepts_int_or_string_count():
    assert Tag.model_validate({"name": "indie", "count": 12}).count == 12
    assert Tag.model_validate({"name": "indie", "count": "5"}).count == 5
    assert Tag.model_validate({"name": "indie"}).count == 0


def test_recent_track_parses_timestamp_and_text_fields():
    track = RecentTrack.model_validate(
        {
            "name": "Song",
            "artist": {"mbid": "", "#text": "IU"},
            "album": {"mbid": "", "#text": "Palette"},
            "date": {"uts": "1700000000", "#text": "14 Nov 2023, 22:13"},
            "mbid": "",
        }
    )
    assert track.artist == "IU"
    assert track.album == "Palette"
    assert track.played_at == datetime.fromtimestamp(1700000000, tz=timezone.utc)
    assert track.mbid is None


def test_recent_track_without_date_is_rejected():
    with pytest.raises(ValidationError):
        RecentTrack.model_validate({"name": "Song", "artist": {"#text": "IU"}})


def test_similar_and_tag_top_artist():
    similar = SimilarArtist.model_validate({"name": "Muse", "match": "0.82", "mbid": ""})
    assert similar.match == pytest.approx(0.82)
    assert TagTopArtist.model_validate({"name": "Muse", "@attr": {"rank": "2"}}).rank == 2


def test_artist_info_flattens_nested_fields():
    info = ArtistInfo.model_validate(
        {
            "name": "Radiohead",
            "mbid": "a74b1b7f",
            "stats": {"listeners": "5000", "playcount": "90000"},
            "tags": {"tag": {"name": "alternative", "url": "u"}},
            "similar": {"artist": [{"name": "Muse"}, {"name": "Thom Yorke"}]},
            "bio": {"summary": 'English band. <a href="https://last.fm">Read more on Last.fm</a>'},
        }
    )
    assert info.listeners == 5000
    assert info.playcount == 90000
    assert info.tags == ["alternative"]
    assert info.similar == ["Muse", "Thom Yorke"]
    assert info.summary == "English band. Read more on Last.fm"


@pytest.mark.parametrize("raw", ["rj", "  some_user-1  ", "a.b"])
def test_validate_username_accepts(raw):
    assert validate_username(raw) == raw.strip()


@pytest.mark.parametrize("raw", ["", "a", "has space", "x" * 33, "user/../etc", "a&method=x"])
def test_validate_username_rejects(raw):
    with pytest.raises(ValueError):
        validate_username(raw)


def test_validate_period():
    assert validate_period("7day") == "7day"
    with pytest.raises(ValueError):
        validate_period("2week")
