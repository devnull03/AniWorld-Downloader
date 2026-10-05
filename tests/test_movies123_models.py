"""Shared framework resolution, lazy metadata, and queue migration."""

import pytest

from aniworld.entry import model_for_url
from aniworld.models import Movies123Episode, Movies123Season, Movies123Series
from aniworld.models.movies123 import source
from aniworld.providers import resolve_provider
from aniworld.web import worker

PATH = "/watch/tv-example-abcd1234"


@pytest.mark.parametrize(
    "suffix, model",
    [("", Movies123Series), ("?s=2", Movies123Season), ("?s=2&e=3", Movies123Episode)],
)
def test_common_entry_selects_registered_model(suffix, model):
    url = source.base_url() + PATH + suffix
    assert resolve_provider(url).name == "Movies123"
    assert isinstance(model_for_url(url), model)


def test_movie_root_is_an_episode():
    assert isinstance(
        model_for_url(source.base_url() + "/watch/movie-example-abcd1234"),
        Movies123Episode,
    )


def test_registration_follows_configured_domain(monkeypatch):
    old = source.base_url() + PATH
    monkeypatch.setenv(source.ENV_KEY, "https://new-domain.example")
    assert resolve_provider("https://new-domain.example" + PATH).name == "Movies123"
    with pytest.raises(ValueError):
        resolve_provider(old)
    with pytest.raises(ValueError):
        resolve_provider("https://unrelated.example" + PATH)
    monkeypatch.setenv(source.ENV_KEY, "http://invalid.example")
    assert (
        resolve_provider("https://aniworld.to/anime/stream/naruto").name == "AniWorld"
    )


def test_series_metadata_and_episode_names_are_lazy(monkeypatch):
    calls = []

    def details(path, season=1, episode=1):
        calls.append(season)
        return {
            "title": "Example",
            "description": "",
            "type": "tv",
            "seasons": [{"number": 1}, {"number": 2}],
            "episodes": [{"number": 1, "title": f"Season {season} premiere"}],
        }

    monkeypatch.setattr(source, "details", details)
    series = Movies123Series(source.base_url() + PATH)
    assert not calls
    seasons = series.seasons
    assert calls == [1]
    assert seasons[0].episodes[0].title_en == "Season 1 premiere"
    assert calls == [1]
    assert seasons[1].episodes[0].title_en == "Season 2 premiere"
    assert calls == [1, 2]
    assert series.seasons is seasons
    assert seasons[1].episode_count == 1
    assert calls == [1, 2]


@pytest.mark.parametrize("legacy", [False, True])
def test_queued_entries_use_registry_after_domain_change(monkeypatch, legacy):
    entry = {
        "url": PATH + "?s=2&e=3",
        "season": 2,
        "episode": 3,
        "selected_provider": "vidrock / Orion",
        "selected_language": "English audio + English subtitles",
        "selected_quality": "720p",
    }
    entry.update(
        {"english_path": PATH}
        if legacy
        else {"source": "movies123", "source_path": PATH}
    )
    monkeypatch.setenv(source.ENV_KEY, "https://new-domain.example")
    url, extra = worker._episode_request(entry)
    assert url.startswith("https://new-domain.example/")
    provider, episode = worker._build_episode(
        url, extra, {"id": 5, "provider": "Vidrock", "language": "Source Audio"}, None
    )
    assert provider.name == "Movies123"
    assert isinstance(episode, Movies123Episode)
    assert episode.selected_provider == "vidrock / Orion"
    assert episode.selected_language == "English audio + English subtitles"
    assert episode.selected_quality == "720p"
    assert episode.queue_id == 5
