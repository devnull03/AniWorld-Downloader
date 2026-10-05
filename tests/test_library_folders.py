"""Cross-source destination matching must preserve identity and user scope."""

import pytest

from aniworld.models.common.library import season_folder, series_folder


def test_reuses_anime_folder_with_year_range_and_provider_id(tmp_path):
    existing = tmp_path / "Anime" / "Bunny Girl Senpai (2018-2025) [imdbid-tt8993398]"
    season = existing / "Season 02"
    season.mkdir(parents=True)
    assert series_folder(tmp_path / "TV", "Bunny Girl Senpai") == existing
    assert season_folder(existing, 2) == season


def test_reuses_tv_from_anime_with_case_and_punctuation_differences(tmp_path):
    existing = tmp_path / "TV" / "Marvel's Agents of S.H.I.E.L.D. (2013)"
    existing.mkdir(parents=True)
    assert series_folder(tmp_path / "Anime", "Marvel’s Agents of SHIELD") == existing


def test_does_not_match_sequels_or_similar_titles(tmp_path):
    (tmp_path / "Anime" / "Bunny Girl Senpai Returns").mkdir(parents=True)
    assert (
        series_folder(tmp_path / "TV", "Bunny Girl Senpai")
        == tmp_path / "TV" / "Bunny Girl Senpai"
    )


def test_explicit_custom_destination_does_not_search_other_libraries(tmp_path):
    (tmp_path / "Anime" / "Example").mkdir(parents=True)
    custom = tmp_path / "Private Downloads"
    assert series_folder(custom, "Example") == custom / "Example"


def test_movies_do_not_count_as_existing_series(tmp_path):
    (tmp_path / "Movies" / "Example").mkdir(parents=True)
    assert series_folder(tmp_path / "TV", "Example") == tmp_path / "TV" / "Example"


def test_ambiguous_series_require_explicit_destination(tmp_path):
    first = tmp_path / "TV" / "Example (2014) [tvdbid-111]"
    second = tmp_path / "Anime" / "Example (2020) [tvdbid-222]"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    with pytest.raises(ValueError, match="Multiple library folders"):
        series_folder(tmp_path / "TV", "Example")
    assert series_folder(second, "Example") == second


def test_ambiguous_seasons_are_not_silently_combined(tmp_path):
    (tmp_path / "Season 2").mkdir()
    (tmp_path / "Season 02").mkdir()
    with pytest.raises(ValueError, match="Multiple folders match season"):
        season_folder(tmp_path, 2)


def test_symlinked_series_do_not_redirect_downloads(tmp_path):
    other = tmp_path / "Elsewhere" / "Example"
    other.mkdir(parents=True)
    root = tmp_path / "TV"
    root.mkdir()
    (root / "Example (2020)").symlink_to(other, target_is_directory=True)
    assert series_folder(root, "Example") == root / "Example"
