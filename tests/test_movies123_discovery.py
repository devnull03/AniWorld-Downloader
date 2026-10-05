"""Discovered choices, refresh behavior, and their download boundaries."""

from types import SimpleNamespace

import pytest

from aniworld.models.common import hls
from aniworld.models.movies123 import discovery
from aniworld.models.movies123.episode import Movies123Episode as EnglishEpisode
from aniworld.models.movies123.source import SourceError

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Japanese",LANGUAGE="ja",DEFAULT=YES,URI="ja.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",LANGUAGE="en",URI="en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720,AUDIO="audio"
720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,AUDIO="audio"
1080.m3u8
"""


def test_tracks_are_parsed_from_actual_master_metadata():
    audio = discovery.audio_tracks(MASTER, "https://cdn.example/master.m3u8")
    quality = discovery.quality_tracks(MASTER, "https://cdn.example/master.m3u8")
    assert [a["label"] for a in audio] == ["Japanese audio", "English audio"]
    assert [q["label"] for q in quality] == ["720p", "1080p"]
    assert quality[0]["uri"] == "https://cdn.example/720.m3u8"


def test_subtitle_metadata_is_not_mistaken_for_audio_or_movie_metadata():
    tracks = discovery.subtitle_tracks(
        {
            "tracks": [
                {"label": "English", "file": "https://cdn.example/en.vtt"},
                {"label": "English", "file": "http://unsafe.example/en.vtt"},
                {"label": "Japanese", "file": "https://cdn.example/video.m3u8"},
            ]
        }
    )
    assert tracks == [{"label": "English", "url": "https://cdn.example/en.vtt"}]


def test_original_audio_and_english_subtitles_are_preferred():
    stream = {
        "audio": discovery.audio_tracks(MASTER, "https://cdn.example/master.m3u8"),
        "subtitles": [{"label": "English", "url": "https://cdn.example/en.vtt"}],
    }
    choices = discovery.options(stream)
    assert (
        discovery.preferred_option(choices, "ja")
        == "Japanese audio + English subtitles"
    )
    assert choices["Japanese audio + English subtitles"]["audio"]["uri"].endswith(
        "/ja.m3u8"
    )


def test_unknown_audio_is_not_labelled_english():
    assert list(discovery.options({"audio": [], "subtitles": []})) == ["Source Audio"]


def test_selected_hls_quality_and_audio_are_honored(monkeypatch, tmp_path):
    monkeypatch.setattr(hls, "_fetch_text", lambda *args: MASTER)
    monkeypatch.setattr(hls, "_publish_progress", lambda **kwargs: None)
    selected = []

    def download(url, headers, prefix, suffix, *args):
        selected.append(url)
        path = tmp_path / suffix
        path.write_bytes(b"video")
        return path

    monkeypatch.setattr(hls, "_download_playlist", download)
    hls.download_hls_parallel(
        "https://cdn.example/master.m3u8",
        tmp_path / "show",
        video_variant_uri="https://cdn.example/720.m3u8",
        audio_rendition_uri="https://cdn.example/ja.m3u8",
    )
    assert selected == ["https://cdn.example/720.m3u8", "https://cdn.example/ja.m3u8"]
    with pytest.raises(hls.HLSUnsupported):
        hls.download_hls_parallel(
            "https://cdn.example/master.m3u8",
            tmp_path / "show",
            video_variant_uri="https://cdn.example/missing.m3u8",
        )


def test_queue_selection_reaches_episode_downloader():
    from aniworld.models.movies123.source import base_url
    from aniworld.web.worker import _build_episode

    _, episode = _build_episode(
        base_url() + "/watch/tv-example-abcd1234?s=2&e=3",
        {
            "selected_provider": "vidlink",
            "selected_language": "Japanese audio + English subtitles",
            "selected_quality": "720p",
        },
        {"id": 1, "language": "Source Audio", "provider": "vidrock"},
        "/media/TV",
    )
    assert (
        episode.selected_provider,
        episode.selected_language,
        episode.selected_quality,
    ) == ("vidlink", "Japanese audio + English subtitles", "720p")


def test_discovery_cache_reports_partial_results_without_rescanning(monkeypatch):
    key = (discovery.base_url(), "/watch/tv-example-abcd1234", 1, 1)
    monkeypatch.setitem(
        discovery._jobs,
        key,
        {
            "pending": True,
            "updated": 0,
            "checked": 1,
            "total": 3,
            "streams": {
                "player": {
                    "audio": [],
                    "subtitles": [],
                    "qualities": [{"label": "1080p", "uri": ""}],
                }
            },
            "errors": {},
        },
    )
    result = discovery.discover(key[1])
    assert result["discovering"] is True
    assert result["providers"] == {"Source Audio": ["player"]}
    assert result["qualities"] == {"player": ["1080p"]}


@pytest.mark.parametrize(
    "kind,initial,expected", [("tv", "Movies", "TV"), ("movie", "TV", "Movies")]
)
def test_default_library_path_routes_media_and_keeps_staging_hidden(
    monkeypatch, tmp_path, kind, initial, expected
):
    from aniworld.models.movies123 import episode as download

    movies, tv = tmp_path / "Movies", tmp_path / "TV"
    movies.mkdir()
    tv.mkdir()
    monkeypatch.setenv("ANIWORLD_DOWNLOAD_PATH", str(tmp_path / initial))
    monkeypatch.setattr(
        download,
        "resolve_stream",
        lambda *args: {
            "title": "Example",
            "type": kind,
            "url": "https://cdn.example/video.m3u8",
            "headers": {},
        },
    )
    paths = []

    def fail(url, prefix, **kwargs):
        paths.append(prefix)
        raise SourceError("stop before network")

    monkeypatch.setattr(download, "download_hls_parallel", fail)
    with pytest.raises(SourceError):
        EnglishEpisode(f"/watch/{kind}-example-abcd1234", 1, 1, None).download()
    output_folder = tmp_path / expected / "Example"
    if kind == "tv":
        output_folder = output_folder / "Season 1"
    assert paths[0].is_relative_to(output_folder / ".download-staging")
    assert not list(output_folder.glob("*.ts"))


@pytest.mark.parametrize("default_failed", [False, True])
def test_scan_enumerates_advertised_players_and_internal_servers(
    monkeypatch, default_failed
):
    import asyncio
    from contextlib import asynccontextmanager

    visited, updates = [], []

    class Browser:
        async def close(self):
            pass

    class Chromium:
        async def launch(self, **kwargs):
            return Browser()

    @asynccontextmanager
    async def runtime():
        yield SimpleNamespace(chromium=Chromium())

    async def probe(browser, server):
        visited.append(server["name"])
        if server["name"] == "vidrock" and default_failed:
            from aniworld.models.movies123.source import SourceUnavailable

            raise SourceUnavailable(
                "Default timed out",
                player_servers=[{"name": "Nova"}, {"name": "Orion"}],
            )
        return {
            "audio": [],
            "subtitles": [],
            "qualities": [],
            "player_servers": [{"name": "Nova"}, {"name": "Orion"}]
            if server["name"] == "vidrock"
            else [],
        }

    monkeypatch.setattr(discovery, "async_playwright", runtime)
    monkeypatch.setattr(discovery, "browser_executable", lambda runtime: "/browser")
    monkeypatch.setattr(discovery, "probe", probe)
    asyncio.run(
        discovery._scan(
            {
                "sources": [
                    {"name": "vidrock", "url": "https://vidrock.net/tv/123/1/1"},
                    {"name": "another", "url": "https://example.com/player"},
                ]
            },
            lambda name, stream, error, **kwargs: updates.append(name),
        )
    )
    assert visited == (
        ["vidrock", "vidrock / Nova", "vidrock / Orion", "another"]
        if default_failed
        else ["vidrock", "vidrock / Orion", "another"]
    )
    assert updates == (
        ["vidrock", "vidrock / Nova", "vidrock / Orion", "another"]
        if default_failed
        else ["vidrock / Nova", "vidrock / Orion", "another"]
    )


def test_scan_yields_between_batches_and_visits_every_advertised_player(monkeypatch):
    path = "/watch/tv-example-abcd1234"
    sources = [
        {"name": name, "url": "https://example.com/" + name}
        for name in ("slow-a", "slow-b", "vidrock", "vidnest", "moviesapi")
    ]
    monkeypatch.setattr(
        discovery,
        "details",
        lambda *args: {"sources": sources, "original_language": "en"},
    )
    queued, visited = [], []
    monkeypatch.setattr(
        discovery,
        "_workers",
        SimpleNamespace(
            submit=lambda callback, *args, **kwargs: queued.append(
                (callback, args, kwargs)
            )
        ),
    )

    async def scan(document, update):
        visited.extend(item["name"] for item in document["sources"])
        for item in document["sources"]:
            update(item["name"], {"audio": [], "subtitles": []}, None)

    monkeypatch.setattr(discovery, "_scan", scan)
    entry = {
        "pending": True,
        "streams": {},
        "errors": {},
        "checked": 0,
        "full_scan": True,
    }
    key = (discovery.base_url(), path, 1, 1)
    discovery._run(key, entry)
    assert visited == ["vidnest", "vidrock"]
    assert entry["pending"] and entry["queued"]
    assert queued[0][2]["priority"] == 1
    while queued:
        callback, args, _kwargs = queued.pop(0)
        callback(*args)
    assert visited == ["vidnest", "vidrock", "moviesapi", "slow-a", "slow-b"]
    assert entry["checked"] == entry["total"] == 5
    assert not entry["pending"]


def test_new_title_jobs_precede_continuations():
    from itertools import count
    from queue import PriorityQueue

    workers = discovery.DiscoveryWorkers.__new__(discovery.DiscoveryWorkers)
    workers.queue, workers.sequence = PriorityQueue(), count()
    background = workers.submit(lambda: None, priority=1)
    fresh = workers.submit(lambda: None)
    assert workers.queue.get()[2] is fresh
    assert workers.queue.get()[2] is background


def test_default_scan_uses_only_advertised_preset_players():
    sources = [
        {"name": name, "url": "https://example.com/" + name}
        for name in ("slow-a", "VidRock", "vidnest", "moviesapi", "vidrift", "slow-b")
    ]
    assert [item["name"] for item in discovery.ordered_players(sources)] == [
        "vidnest",
        "VidRock",
        "moviesapi",
        "vidrift",
    ]
    assert len(discovery.ordered_players(sources, full_scan=True)) == 6


def test_full_and_preset_discovery_have_separate_caches(monkeypatch):
    queued = []
    monkeypatch.setattr(discovery, "_jobs", {})
    monkeypatch.setattr(
        discovery, "_workers", SimpleNamespace(submit=lambda *args: queued.append(args))
    )
    path = "/watch/tv-example-abcd1234"
    assert discovery.discover(path)["scan_mode"] == "preset"
    assert discovery.discover(path, full_scan=True)["scan_mode"] == "all"
    assert len(queued) == 2
    discovery.discover(path)
    assert len(queued) == 2
