"""123Movies participates in the existing home-page API contracts."""

import json
from types import SimpleNamespace

import pytest

from aniworld import english_source as source
from aniworld.web import db, english_adapter, sitesearch

PATH = "/watch/tv-example-abcd1234"
WATCH = """<script type="application/ld+json">{"@type":"TVSeries","name":"Example","description":"A show."}</script>
<script>window.__OPT=["https://vidrock.net/tv/123/1/1"];</script>
<button class="opt-pill" data-i="0">vidrock</button>
<a class="season-pill" href="/watch/tv-example-abcd1234?s=1&amp;e=1">S1</a>
<a class="season-pill" href="/watch/tv-example-abcd1234?s=2&amp;e=1">S2</a>
<a class="ep-card" title="E1 · First" href="/watch/tv-example-abcd1234?s=1&amp;e=1">1</a>
<a class="ep-card" title="E2 · Second" href="/watch/tv-example-abcd1234?s=1&amp;e=2">2</a>"""


@pytest.fixture
def watch(monkeypatch):
    monkeypatch.setattr(source, "fetch", lambda *args: SimpleNamespace(text=WATCH))
    from aniworld import english_discovery

    monkeypatch.setattr(
        english_discovery,
        "discover",
        lambda *args: {
            "providers": {"Source Audio": ["Vidrock"]},
            "qualities": {},
            "discovering": False,
            "checked": 1,
            "total": 1,
        },
    )


def test_shared_title_modal_contracts(client, watch):
    url = source.base_url() + PATH
    assert (
        client.get("/api/series", query_string={"url": url}).get_json()["title"]
        == "Example"
    )
    seasons = client.get("/api/seasons", query_string={"url": url}).get_json()[
        "seasons"
    ]
    assert [entry["season_number"] for entry in seasons] == [1, 2]
    episodes = client.get(
        "/api/episodes", query_string={"url": seasons[0]["url"]}
    ).get_json()["episodes"]
    assert [entry["title_en"] for entry in episodes] == ["First", "Second"]
    assert client.get(
        "/api/providers", query_string={"url": episodes[0]["url"]}
    ).get_json()["providers"] == {"Source Audio": ["Vidrock"]}


def test_full_scan_is_explicit_and_invalid_modes_are_rejected(
    client, watch, monkeypatch
):
    from aniworld import english_discovery

    calls = []
    monkeypatch.setattr(
        english_discovery,
        "discover",
        lambda *args: calls.append(args) or {"providers": {}, "discovering": False},
    )
    url = source.base_url() + PATH
    assert client.get("/api/providers", query_string={"url": url}).status_code == 200
    assert calls[-1][-1] is False
    assert (
        client.get(
            "/api/providers", query_string={"url": url, "scan": "all"}
        ).status_code
        == 200
    )
    assert calls[-1][-1] is True
    assert (
        client.get(
            "/api/providers", query_string={"url": url, "scan": "invalid"}
        ).status_code
        == 400
    )
    assert len(calls) == 2


def test_shared_download_queues_multiple_seasons(client, watch):
    url = source.base_url() + PATH
    response = client.post(
        "/api/download",
        json={
            "series_url": url,
            "provider": "Vidrock",
            "episodes": [url + "?s=1&e=2", url + "?s=2&e=1", url + "?s=1&e=2"],
        },
    )
    assert response.status_code == 200
    item = db.get_queue_item(response.get_json()["queue_id"])
    entries = json.loads(item["episodes"])
    assert [(entry["season"], entry["episode"]) for entry in entries] == [
        (1, 2),
        (2, 1),
    ]
    assert item["series_url"] == PATH
    assert item["language"] == "Source Audio"


@pytest.mark.parametrize(
    "episode", ["?s=1&e=3", "?s=1&e=-1", "?s=oops&e=1", "/foreign"]
)
def test_shared_download_rejects_unavailable_or_malformed_selection(
    client, watch, episode
):
    url = source.base_url() + PATH
    assert (
        client.post(
            "/api/download",
            json={
                "series_url": url,
                "provider": "Vidrock",
                "episodes": [url + episode],
            },
        ).status_code
        == 400
    )
    assert db.get_next_queued() is None


def test_movie_uses_existing_movie_season_and_episode_shape(client, monkeypatch):
    movie = WATCH.replace("TVSeries", "Movie").replace(
        PATH, "/watch/movie-example-abcd1234"
    )
    monkeypatch.setattr(source, "fetch", lambda *args: SimpleNamespace(text=movie))
    url = source.base_url() + "/watch/movie-example-abcd1234"
    season = client.get("/api/seasons", query_string={"url": url}).get_json()["seasons"]
    assert len(season) == 1 and season[0]["are_movies"]
    episodes = client.get(
        "/api/episodes", query_string={"url": season[0]["url"]}
    ).get_json()["episodes"]
    assert len(episodes) == 1 and episodes[0]["title_en"] == "Example"


def test_shared_search_and_source_switch(client, monkeypatch):
    monkeypatch.setitem(
        sitesearch.SITE_SEARCH,
        "movies123",
        lambda keyword: [
            {"title": "Example", "url": source.base_url() + PATH, "poster": ""}
        ],
    )
    assert (
        client.post(
            "/api/search", json={"site": "movies123", "keyword": "example"}
        ).get_json()["results"][0]["title"]
        == "Example"
    )
    assert 'data-site="movies123"' in client.get("/").get_data(as_text=True)
    monkeypatch.setenv("ANIWORLD_ENABLE_MOVIES123", "0")
    assert 'data-site="movies123"' not in client.get("/").get_data(as_text=True)


def test_source_adapter_rejects_other_origins():
    assert not english_adapter.is_source_url("https://example.com" + PATH)
    with pytest.raises(source.SourceError):
        english_adapter.selection("https://example.com" + PATH)
