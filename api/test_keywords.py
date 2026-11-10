"""Tests for keyword normalization (no DB)."""
from keywords import normalize_keyword


def test_normalize_strips_hash_and_case():
    assert normalize_keyword("  #AI Art ") == "ai art"


def test_normalize_collapses_whitespace():
    assert normalize_keyword("dark   humor\tmemes") == "dark humor memes"


def test_normalize_empty():
    assert normalize_keyword("  # ") == ""


def test_normalize_caps_length():
    assert len(normalize_keyword("x" * 200)) == 50


def test_instagram_channels_search_tiktok_and_youtube():
    from keywords import search_platforms_for
    assert search_platforms_for("instagram") == ("tiktok", "youtube")
    assert search_platforms_for("youtube") == ("youtube",)
    assert search_platforms_for("tiktok") == ("tiktok",)
