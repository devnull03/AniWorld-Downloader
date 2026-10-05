"""Exercise actual transient failures and shared server backoff without network."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import niquests
import pytest

from aniworld import english_browser, english_source
from aniworld.english_download import EnglishEpisode
from aniworld.models.common import hls


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    waits = []

    def sleep(delay):
        waits.append(delay)
        now[0] += delay

    monkeypatch.setattr(hls.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(hls.time, "sleep", sleep)
    monkeypatch.setattr(hls.random, "uniform", lambda *args: 0)
    monkeypatch.setattr(hls, "_cooldowns", {})
    return now, waits


def responses(monkeypatch, statuses):
    calls, closed = [], []
    items = iter(statuses)

    def get(url, **kwargs):
        status, retry = next(items)
        calls.append(url)
        response = SimpleNamespace(
            status_code=status,
            headers={"Retry-After": retry},
            content=b"segment",
            text="#EXTM3U",
            close=lambda: closed.append(status),
        )

        def raise_for_status():
            if status >= 400:
                raise niquests.HTTPError(
                    "signed URL should stay private", response=response
                )

        response.raise_for_status = raise_for_status
        return response

    monkeypatch.setattr(hls, "_session", lambda: SimpleNamespace(get=get))
    return calls, closed


def test_rate_limit_cooldown_is_shared_by_host(monkeypatch, clock):
    calls, closed = responses(monkeypatch, [(429, "7")] * 3 + [(200, None)] * 2)
    with pytest.raises(RuntimeError, match="HTTP 429"):
        hls._fetch_bytes("https://cdn.example/a.ts?token=secret", {})
    assert clock[1] == [7, 7]
    # Another host can proceed; another worker on the same host must wait.
    assert hls._fetch_text("https://other.example/master.m3u8", {}) == "#EXTM3U"
    assert clock[1] == [7, 7]
    assert hls._fetch_bytes("https://cdn.example/b.ts", {}) == b"segment"
    assert clock[1] == [7, 7, 7]
    assert len(calls) == len(closed) == 5


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_permanent_http_failure_is_not_retried(monkeypatch, clock, status):
    calls, closed = responses(monkeypatch, [(status, None)])
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        hls._fetch_bytes("https://cdn.example/a.ts?token=secret", {})
    assert "secret" not in str(error.value)
    assert len(calls) == len(closed) == 1
    assert clock[1] == []


def test_playlist_transient_failure_is_retried(monkeypatch, clock):
    responses(monkeypatch, [(503, "3"), (200, None)])
    assert hls._fetch_text("https://cdn.example/master.m3u8", {}) == "#EXTM3U"
    assert clock[1] == [3]


def test_long_retry_after_stops_without_retrying_early(monkeypatch, clock):
    calls, _ = responses(monkeypatch, [(429, "120")])
    with pytest.raises(RuntimeError, match="longer cooldown"):
        hls._fetch_bytes("https://cdn.example/a.ts", {})
    assert len(calls) == 1
    assert clock[1] == []
    with pytest.raises(RuntimeError, match="longer cooldown"):
        hls._fetch_bytes("https://cdn.example/b.ts", {})
    assert len(calls) == 1


def test_retry_after_http_date_and_malformed_values():
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=20))
    assert 18 <= hls._retry_after(future) <= 20
    assert hls._retry_after("bad") is None
    assert hls._retry_after("nan") is None
    assert hls._retry_after("inf") is None
    assert hls._retry_after(None) is None


@pytest.mark.parametrize(
    "playlist, usable",
    [
        ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5000\n720.m3u8", True),
        ("#EXTM3U\n#EXTINF:5,\n1.ts\n#EXT-X-ENDLIST", True),
        ("#EXTM3U\n#EXTINF:5,\n1.ts", False),
        ("<html>429</html>", False),
    ],
)
def test_legacy_resolver_accepts_quality_master(playlist, usable):
    assert english_browser.usable_playlist(playlist) is usable


def test_resolution_retries_transient_failure_with_fresh_attempt(monkeypatch, clock):
    episode = EnglishEpisode("/watch/tv-test-123", 1, 2, None)
    calls = []

    def resolve():
        calls.append(1)
        if len(calls) < 3:
            raise english_source.SourceUnavailable("player timeout")
        return {"url": "fresh"}

    monkeypatch.setattr(episode, "_resolve_once", resolve)
    assert episode._resolve_with_retry() == {"url": "fresh"}
    assert len(calls) == 3
    assert sum(clock[1]) == 6


def test_invalid_choices_are_not_retried(monkeypatch, clock):
    episode = EnglishEpisode("/watch/tv-test-123", 1, 2, None)
    calls = []

    def resolve():
        calls.append(1)
        raise english_source.SourceError("selected audio unavailable")

    monkeypatch.setattr(episode, "_resolve_once", resolve)
    with pytest.raises(english_source.SourceError):
        episode._resolve_with_retry()
    assert len(calls) == 1
    assert clock[1] == []


def test_cancellation_interrupts_resolution_backoff(monkeypatch, clock):
    episode = EnglishEpisode("/watch/tv-test-123", 1, 2, None)

    def resolve():
        raise english_source.SourceUnavailable("player timeout")

    def check():
        if clock[1]:
            raise english_source.SourceError("Download cancelled.")

    monkeypatch.setattr(episode, "_resolve_once", resolve)
    monkeypatch.setattr(episode, "_check_cancelled", check)
    with pytest.raises(english_source.SourceError, match="cancelled"):
        episode._resolve_with_retry()
    assert sum(clock[1]) == 0.25


def test_connection_timeout_uses_bounded_backoff(monkeypatch, clock):
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        raise niquests.Timeout("timeout")

    monkeypatch.setattr(hls, "_session", lambda: SimpleNamespace(get=get))
    with pytest.raises(RuntimeError, match="3 attempts"):
        hls._fetch_bytes("https://cdn.example/a.ts", {})
    assert len(calls) == 3
    assert clock[1] == [1, 2]


@pytest.mark.parametrize("delay, expected_attempts", [(5, 2), (120, 1)])
def test_player_retry_respects_server_delay(
    monkeypatch, clock, delay, expected_attempts
):
    episode = EnglishEpisode("/watch/tv-test-123", 1, 2, None)
    response = SimpleNamespace(
        status=429,
        url="https://player.example/watch?token=secret",
        headers={"Retry-After": str(delay)},
        request=SimpleNamespace(resource_type="document"),
    )
    failure = english_browser.player_http_failure(response)
    assert "HTTP 429" in str(failure)
    assert "secret" not in str(failure)
    calls = []

    def resolve():
        calls.append(1)
        if len(calls) == 1:
            raise failure
        return {"url": "fresh"}

    monkeypatch.setattr(episode, "_resolve_once", resolve)
    if delay > 60:
        with pytest.raises(english_source.SourceUnavailable):
            episode._resolve_with_retry()
        assert not clock[1]
    else:
        assert episode._resolve_with_retry() == {"url": "fresh"}
        assert sum(clock[1]) == delay
    assert len(calls) == expected_attempts


def test_source_concurrency_cap_preserves_lower_user_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hls, "_fetch_text", lambda *args: "#EXTM3U\n#EXTINF:5,\n1.ts\n#EXT-X-ENDLIST"
    )
    monkeypatch.setattr(hls, "_fetch_bytes", lambda *args: b"video")
    monkeypatch.setattr(hls, "_publish_progress", lambda **kwargs: None)
    monkeypatch.setattr(hls._ProgressTracker, "advance", lambda *args: None)
    pool_sizes = []
    real_pool = hls.ThreadPoolExecutor

    def pool(max_workers):
        pool_sizes.append(max_workers)
        return real_pool(max_workers=max_workers)

    monkeypatch.setattr(hls, "ThreadPoolExecutor", pool)
    for configured in (8, 2, 1):
        monkeypatch.setattr(hls, "get_concurrency", lambda value=configured: value)
        files = hls.download_hls_parallel(
            "https://cdn.example/show.m3u8",
            tmp_path / "episode",
            segment_transform=lambda data: data,
            concurrency_limit=3,
        )
        assert files[0].read_bytes() == b"video"
    assert pool_sizes == [3, 2]
