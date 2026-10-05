"""Episode parsing and the real queue/download boundaries, without network."""

import json
from types import SimpleNamespace

import pytest

from aniworld.models.movies123 import source
from aniworld.models.movies123.episode import Movies123Episode as EnglishEpisode
from aniworld.models.movies123.episode import normalize_segment
from aniworld.web import db, worker

WATCH = """<script type="application/ld+json">{"@type":"TVSeries","name":"Reacher","inLanguage":"en"}</script>
<script>window.__OPT=["https://vidsrc.mov/embed/tv/108978/1/1","https://vidrock.net/tv/108978/1/1"];</script>
<button class="opt-pill" data-i="0">vidsrc.mov</button><button class="opt-pill" data-i="1">vidrock</button>
<a class="season-pill" href="/watch/tv-reacher-2pzovm4q?s=1&amp;e=1">S1</a>
<a class="season-pill" href="/watch/tv-reacher-2pzovm4q?s=2&amp;e=1">S2</a>
<a class="ep-card" title="E1 · Welcome to Margrave" href="/watch/tv-reacher-2pzovm4q?s=1&amp;e=1">1</a>
<a class="ep-card" title="E2 · First Dance" href="/watch/tv-reacher-2pzovm4q?s=1&amp;e=2">2</a>
<span class="ep-card is-upcoming">Future episode</span>"""
PATH = "/watch/tv-reacher-2pzovm4q"


def download_request(client, payload, **kwargs):
    path = payload.get("path", "")
    season = payload.get("season", 1)
    episodes = payload.get("episodes", [])
    valid = (
        isinstance(path, str)
        and source.TITLE_PATH.fullmatch(path)
        and type(season) is int
        and isinstance(episodes, list)
        and all(type(number) is int for number in episodes)
    )
    return client.post(
        "/api/download",
        json={
            "site": "movies123",
            "series_url": source.base_url() + path if valid else "invalid",
            "episodes": [
                source.base_url() + path + f"?s={season}&e={number}"
                for number in episodes
            ]
            if valid
            else ["invalid"],
            "provider": "vidrock",
            "language": "Source Audio",
            "custom_path_id": payload.get("custom_path_id"),
        },
        **kwargs,
    )


@pytest.fixture
def source_details(monkeypatch):
    monkeypatch.setattr(source, "fetch", lambda *args: SimpleNamespace(text=WATCH))


def test_observed_seasons_episodes_and_server_mapping(source_details):
    item = source.details(PATH)
    assert [season["number"] for season in item["seasons"]] == [1, 2]
    assert [episode["title"] for episode in item["episodes"]] == [
        "Welcome to Margrave",
        "First Dance",
    ]
    assert item["sources"][1] == {
        "name": "vidrock",
        "url": "https://vidrock.net/tv/108978/1/1",
    }
    assert item["download_supported"] is True


def test_catalog_trailer_warning_is_separate_from_episode_title(monkeypatch, client):
    monkeypatch.setattr(
        source,
        "fetch",
        lambda *args: SimpleNamespace(
            text=WATCH.replace(
                "E1 · Welcome to Margrave", "E1 · Welcome to Margrave (Trailer only)"
            )
        ),
    )
    item = source.details(PATH)
    assert item["episodes"][0]["title"] == "Welcome to Margrave"
    assert item["episodes"][0]["availability_hint"] == "trailer_only"
    result = client.get(
        "/api/episodes", query_string={"url": source.DEFAULT_BASE_URL + PATH}
    )
    episode = result.get_json()["episodes"][0]
    assert episode["title_en"] == "Welcome to Margrave"
    assert episode["availability_hint"] == "trailer_only"


def test_episode_scraper_uses_browser_only_for_verification(monkeypatch):
    from aniworld.models.movies123 import browser as english_browser

    def fetch(*args):
        raise source.SourceError("The source requires browser verification.")

    calls = []
    monkeypatch.setattr(source, "fetch", fetch)
    monkeypatch.setattr(
        english_browser, "watch_html", lambda url: calls.append(url) or WATCH
    )
    assert len(source.details(PATH, 2)["episodes"]) == 2
    assert calls == [source.DEFAULT_BASE_URL + PATH + "?s=2&e=1"]


def test_missing_player_is_an_error(monkeypatch):
    monkeypatch.setattr(
        source, "fetch", lambda *args: SimpleNamespace(text="<h1>Quick check</h1>")
    )
    with pytest.raises(source.SourceError, match="unsupported"):
        source.details(PATH)


def test_details_do_not_accept_arbitrary_urls():
    with pytest.raises(source.SourceError):
        source.details("https://internal.example/admin")


def test_episode_selection_enters_existing_queue(client, source_details):
    result = download_request(
        client, {"path": PATH, "season": 1, "episodes": [2, 1, 2]}
    )
    assert result.status_code == 200
    item = db.get_queue_item(result.get_json()["queue_id"])
    entries = json.loads(item["episodes"])
    assert [entry["episode"] for entry in entries] == [2, 1]
    assert all(entry["source_path"] == PATH for entry in entries)
    assert item["provider"] == "vidrock"


def test_internal_provider_can_be_queued(client, source_details):
    url = source.DEFAULT_BASE_URL + PATH
    result = client.post(
        "/api/download",
        json={
            "series_url": url,
            "episodes": [url + "?s=1&e=2"],
            "provider": "vidrock / Orion",
            "language": "English audio + English subtitles",
            "quality": "720p",
        },
    )
    assert result.status_code == 200
    entries = json.loads(db.get_queue_item(result.get_json()["queue_id"])["episodes"])
    assert entries[0]["selected_provider"] == "vidrock / Orion"


@pytest.mark.parametrize(
    "payload",
    [
        {"path": PATH, "episodes": []},
        {"path": PATH, "episodes": [True]},
        {"path": PATH, "episodes": [3]},
        {"path": PATH, "episodes": [-1]},
        {"path": PATH, "episodes": [1], "season": "1"},
        {"path": PATH, "episodes": [1], "custom_path_id": 999},
        {"path": "https://example.com/watch/tv-reacher-2pzovm4q", "episodes": [1]},
    ],
)
def test_invalid_selection_never_queues(client, source_details, payload):
    assert download_request(client, payload).status_code == 400
    assert db.get_next_queued() is None


def test_read_key_cannot_start_download(client, source_details, api_key):
    raw, _ = api_key(scope="read")
    result = download_request(
        client,
        {"path": PATH, "episodes": [1]},
        headers={"X-API-Key": raw},
    )
    assert result.status_code == 403
    assert db.get_next_queued() is None


def test_queue_worker_uses_relative_title_after_domain_change(
    client, source_details, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        EnglishEpisode,
        "download",
        lambda self: calls.append((self.path, self.season_number, self.episode)),
    )
    response = download_request(client, {"path": PATH, "season": 1, "episodes": [2]})
    monkeypatch.setenv(source.ENV_KEY, "https://new-domain.example")
    queue_id = response.get_json()["queue_id"]
    worker._process(db.get_queue_item(queue_id))
    assert calls == [(PATH, 1, 2)]
    assert db.get_queue_item(queue_id)["status"] == "completed"


def test_queue_records_download_failure(client, source_details, monkeypatch):
    def fail(self):
        raise source.SourceError("No playable stream.")

    monkeypatch.setattr(EnglishEpisode, "download", fail)
    response = download_request(client, {"path": PATH, "episodes": [1]})
    queue_id = response.get_json()["queue_id"]
    worker._process(db.get_queue_item(queue_id))
    assert db.get_queue_item(queue_id)["status"] == "failed"


def test_png_wrapped_transport_stream_is_normalized():
    transport = (b"G" + b"\x00" * 187) * 6
    wrapped = b"\x89PNG\r\n\x1a\n" + b"\x00" * 62 + transport
    assert normalize_segment(wrapped) == transport
    assert normalize_segment(transport) == transport


def test_image_without_video_is_not_downloaded_as_success():
    with pytest.raises(source.SourceError, match="image without"):
        normalize_segment(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000)


@pytest.mark.parametrize("concurrency", [1, 4])
def test_hls_applies_segment_transform_and_validation_limit(
    monkeypatch, tmp_path, concurrency
):
    from aniworld.models.common import hls

    monkeypatch.setattr(hls, "get_concurrency", lambda: concurrency)

    monkeypatch.setattr(
        hls,
        "_fetch_text",
        lambda *args: "#EXTM3U\n#EXTINF:5,\n1.ts\n#EXTINF:5,\n2.ts\n#EXT-X-ENDLIST\n",
    )
    fetched = []

    def fetch(url, headers):
        fetched.append(url)
        return b"prefix-video"

    monkeypatch.setattr(hls, "_fetch_bytes", fetch)
    monkeypatch.setattr(hls, "_publish_progress", lambda **kwargs: None)
    monkeypatch.setattr(hls._ProgressTracker, "advance", lambda *args: None)
    files = hls.download_hls_parallel(
        "https://cdn.example/show.m3u8",
        tmp_path / "episode",
        segment_transform=lambda data: data.removeprefix(b"prefix-"),
        segment_limit=1,
    )
    assert files[0].read_bytes() == b"video"
    assert fetched == ["https://cdn.example/1.ts"]
