"""Discover playable HLS, declared audio, and subtitles from advertised players."""

import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future
from itertools import count
from pathlib import Path
from queue import PriorityQueue
from urllib.parse import urljoin, urlsplit

import niquests
from patchright.async_api import Error, async_playwright

from .english_browser import browser_executable, player_http_failure
from .english_source import (
    SourceError,
    SourceUnavailable,
    _public_host,
    base_url,
    details,
)
from .models.common.hls import _parse_attributes

_CACHE_TTL = 300
PRESET_PLAYERS = ("vidnest", "vidrock", "moviesapi", "vidrift")
_jobs = {}
_lock = threading.Lock()


class DiscoveryWorkers:
    """Two bounded workers; new titles precede further batches of older scans."""

    def __init__(self):
        self.queue = PriorityQueue()
        self.sequence = count()
        for index in range(2):
            threading.Thread(
                target=self._work, name=f"source-discovery-{index}", daemon=True
            ).start()

    def submit(self, callback, *args, priority=0):
        future = Future()
        self.queue.put((priority, next(self.sequence), future, callback, args))
        return future

    def _work(self):
        while True:
            _, _, future, callback, args = self.queue.get()
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(callback(*args))
                    except Exception as exc:
                        future.set_exception(exc)
            finally:
                self.queue.task_done()


_workers = DiscoveryWorkers()


def ordered_players(sources, full_scan=False):
    """Default to verified integrations; include all others only on request."""
    first = PRESET_PLAYERS
    return sorted(
        sources
        if full_scan
        else [item for item in sources if item["name"].casefold() in first],
        key=lambda item: (
            first.index(item["name"].casefold())
            if item["name"].casefold() in first
            else len(first)
        ),
    )


def subtitle_tracks(value):
    """Read player track JSON, without confusing audio/film metadata with captions."""
    found = []
    if isinstance(value, list):
        for item in value:
            found.extend(subtitle_tracks(item))
    elif isinstance(value, dict):
        url = value.get("file") or value.get("src") or value.get("url")
        label = value.get("label") or value.get("language") or value.get("srclang")
        if (
            isinstance(url, str)
            and isinstance(label, str)
            and re.search(r"\.(vtt|srt)(?:[?#]|$)", url, re.IGNORECASE)
            and urlsplit(url).scheme == "https"
        ):
            found.append({"url": url, "label": label})
        for item in value.values():
            if isinstance(item, (list, dict)):
                found.extend(subtitle_tracks(item))
    return found


def audio_tracks(text, origin):
    tracks = []
    for line in text.splitlines():
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = _parse_attributes(line)
        if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
            tracks.append(
                {
                    "label": (attrs.get("NAME") or attrs.get("LANGUAGE") or "Audio")
                    + " audio",
                    "language": attrs.get("LANGUAGE", ""),
                    "uri": urljoin(origin, attrs["URI"]),
                }
            )
    return tracks


def quality_tracks(text, origin):
    lines = [line.strip() for line in text.splitlines()]
    tracks = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _parse_attributes(line)
        uri = next(
            (line for line in lines[index + 1 :] if line and not line.startswith("#")),
            "",
        )
        resolution = attrs.get("RESOLUTION", "")
        label = (
            resolution.split("x")[-1] + "p"
            if re.fullmatch(r"\d+x\d+", resolution)
            else attrs.get("NAME") or attrs.get("BANDWIDTH", "") + " bps"
        )
        if uri:
            tracks.append({"label": label, "uri": urljoin(origin, uri)})
    return tracks


def options(stream):
    audio = stream.get("audio") or [
        {"label": "Source Audio", "language": "", "uri": ""}
    ]
    english = next(
        (
            track
            for track in stream.get("subtitles", [])
            if track["label"].lower() in ("english", "en", "eng")
        ),
        None,
    )
    if english is None:
        english = next(
            (
                track
                for track in stream.get("subtitles", [])
                if track["label"].lower().startswith("english")
            ),
            None,
        )
    choices = {}
    for track in audio:
        choices[track["label"]] = {"audio": track, "subtitle": None}
        if english:
            choices[track["label"] + " + English subtitles"] = {
                "audio": track,
                "subtitle": english,
            }
    return choices


def _inspect_plain_media(stream, body):
    """Inspect one plain segment when the player omits resolution metadata."""
    if "#EXT-X-KEY" in body:
        return
    segment = next(
        (
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.startswith("#")
        ),
        None,
    )
    if not segment:
        return
    url = urljoin(stream["url"], segment)
    if urlsplit(url).scheme != "https":
        return
    _public_host(url)
    from .english_download import normalize_segment

    with niquests.Session() as session:
        session.trust_env = False
        response = session.get(
            url,
            headers=stream["headers"],
            timeout=15,
            stream=True,
            allow_redirects=False,
        )
        try:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    return
                chunks.append(chunk)
            data = normalize_segment(b"".join(chunks))
        finally:
            response.close()
    staging = (
        Path(os.environ.get("ANIWORLD_DOWNLOAD_PATH") or tempfile.gettempdir())
        / ".download-staging"
    )
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="source-quality-", dir=staging) as folder:
        sample = Path(folder) / "segment.ts"
        sample.write_bytes(data)
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,width,height:stream_tags=language",
                "-of",
                "json",
                str(sample),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
    height = next(
        (
            item.get("height")
            for item in streams
            if item.get("codec_type") == "video" and item.get("height")
        ),
        None,
    )
    if height:
        stream["qualities"] = [{"label": f"{height}p", "uri": ""}]


def preferred_option(mapping, original_language):
    language = str(original_language or "").lower().split("-")[0]
    names = {
        "en": "english",
        "eng": "english",
        "ja": "japanese",
        "jpn": "japanese",
        "fr": "french",
        "de": "german",
        "es": "spanish",
        "it": "italian",
        "ko": "korean",
        "zh": "chinese",
    }
    wanted = names.get(language, language)
    return (
        next(
            (
                label
                for label in mapping
                if wanted
                and label.lower().startswith(wanted)
                and label.endswith("+ English subtitles")
            ),
            None,
        )
        or next(
            (label for label in mapping if label == "Source Audio + English subtitles"),
            None,
        )
        or next(
            (label for label in mapping if label.endswith("+ English subtitles")), None
        )
        or next(iter(mapping), "")
    )


async def probe(browser, server, timeout=12):
    target = server["url"]
    await asyncio.to_thread(_public_host, target)
    context = await browser.new_context()
    page = await context.new_page()
    captured, subtitles, declared_audio, tasks = [], [], set(), set()
    failures = []
    player_servers = []
    selected_ready = not server.get("player_server")
    eligible_requests = set()
    checked = set()
    host = urlsplit(target).hostname

    async def route_request(route):
        request = route.request
        parts = urlsplit(request.url)
        if request.frame != page.main_frame or request.resource_type in (
            "image",
            "media",
            "font",
        ):
            await route.abort()
            return
        if request.resource_type in ("document", "script") and parts.hostname not in (
            host,
            "cdn.jsdelivr.net",
            "cdnjs.cloudflare.com",
        ):
            await route.abort()
            return
        try:
            if parts.hostname not in checked:
                await asyncio.to_thread(_public_host, request.url)
                checked.add(parts.hostname)
            await route.continue_()
        except (SourceError, Error):
            await route.abort()

    async def capture(response):
        try:
            if response.frame == page.main_frame:
                failure = player_http_failure(response)
                if failure:
                    failures.append(failure)
            if response.status != 200 or response.frame != page.main_frame:
                return
            kind = response.headers.get("content-type", "").lower()
            if ".m3u8" in response.url or "mpegurl" in kind:
                if response.request not in eligible_requests:
                    return
                body = await response.text()
                if not body.lstrip().startswith("#EXTM3U") or (
                    "#EXT-X-STREAM-INF" not in body and "#EXT-X-ENDLIST" not in body
                ):
                    return
                if re.search(
                    r"#EXT-X-KEY:.*METHOD=(?!AES-128(?:,|$)|NONE(?:,|$))", body
                ):
                    return
                headers = await response.request.all_headers()
                captured.append(
                    {
                        "url": response.url,
                        "headers": {
                            key: value
                            for key, value in headers.items()
                            if key in ("referer", "origin", "user-agent")
                        },
                        "audio": audio_tracks(body, response.url),
                        "qualities": quality_tracks(body, response.url),
                        "playlist_text": body,
                    }
                )
            elif "json" in kind:
                value = await response.json()
                subtitles.extend(subtitle_tracks(value))
                # Vidrock explicitly describes its playable servers' audio.
                if (
                    urlsplit(response.url).hostname == host
                    and "/api/" in response.url
                    and isinstance(value, dict)
                ):
                    for name, item in value.items():
                        if (
                            isinstance(item, dict)
                            and item.get("url")
                            and item.get("type") == "hls"
                            and isinstance(item.get("language"), str)
                        ):
                            if host == "vidrock.net":
                                player_servers.append(
                                    {"name": name, "language": item["language"]}
                                )
                            if server.get("player_server") == name or (
                                not server.get("player_server")
                                and len(player_servers) <= 1
                            ):
                                declared_audio.add(item["language"])
        except (Error, ValueError):
            pass

    def schedule(response):
        task = asyncio.create_task(capture(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        await page.route("**/*", route_request)
        page.on("response", schedule)
        page.on(
            "request",
            lambda request: eligible_requests.add(request) if selected_ready else None,
        )
        page.on("popup", lambda popup: asyncio.create_task(popup.close()))
        await page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)
        if server.get("player_server"):
            if host != "vidrock.net":
                raise SourceError(
                    "This player does not expose selectable internal servers."
                )
            menu = page.get_by_title("Server List", exact=True)
            await menu.wait_for(state="visible", timeout=timeout * 1000)
            await menu.evaluate("(button) => button.click()")
            choice = page.get_by_text(server["player_server"], exact=True)
            await choice.wait_for(state="visible", timeout=timeout * 1000)
            selected_ready = True
            failures.clear()
            if player_servers and player_servers[0]["name"] == server["player_server"]:
                # Clicking the already active default does not reload the video.
                await page.reload(wait_until="domcontentloaded", timeout=timeout * 1000)
            else:
                await choice.evaluate("(element) => element.click()")
        deadline = time.monotonic() + timeout
        while not captured and not failures and time.monotonic() < deadline:
            await page.wait_for_timeout(200)
        if not captured:
            if failures:
                raise max(failures, key=lambda error: error.retry_after)
            raise SourceUnavailable(
                "This player did not return a supported playlist.",
                player_servers=player_servers,
            )
        # Caption metadata can arrive immediately after the video playlist.
        await page.wait_for_timeout(1500)
        if tasks:
            await asyncio.wait(list(tasks), timeout=2)
        dom = await page.locator("track").evaluate_all(
            "(nodes) => nodes.map(n => ({src:n.src,label:n.label || n.srclang}))"
        )
        subtitles.extend(subtitle_tracks(dom))
        stream = next((item for item in captured if item["qualities"]), captured[0])
        if not stream["audio"] and len(declared_audio) == 1:
            stream["audio"] = [
                {
                    "label": next(iter(declared_audio)) + " audio",
                    "language": "",
                    "uri": "",
                }
            ]
        if not stream["qualities"]:
            try:
                await asyncio.to_thread(
                    _inspect_plain_media, stream, stream["playlist_text"]
                )
            except Exception:
                pass
        stream.pop("playlist_text", None)
        stream["provider"] = server["name"]
        stream["player_servers"] = list(
            {item["name"]: item for item in player_servers}.values()
        )
        stream["subtitles"] = list({item["url"]: item for item in subtitles}.values())
        return stream
    except Error:
        if failures:
            raise max(failures, key=lambda error: error.retry_after) from None
        raise SourceUnavailable("The advertised player could not be opened.") from None
    finally:
        for task in tasks:
            task.cancel()
        await context.close()


async def _scan(document, update):
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(
            executable_path=browser_executable(runtime), headless=True
        )
        semaphore = asyncio.Semaphore(2)

        async def one(server):
            async with semaphore:
                stream = None
                variants = []
                try:
                    stream = await probe(browser, server)
                    variants = stream.get("player_servers", [])
                    label = (
                        server["name"] + " / " + variants[0]["name"]
                        if variants
                        else server["name"]
                    )
                    update(label, stream, None)
                except Exception as exc:
                    variants = getattr(exc, "player_servers", [])
                    update(server["name"], None, str(exc))
                # A dead default must not hide the other advertised servers.
                for variant in variants[1:] if stream else variants:
                    label = server["name"] + " / " + variant["name"]
                    try:
                        extra = await probe(
                            browser,
                            {**server, "name": label, "player_server": variant["name"]},
                        )
                        update(label, extra, None, extra=True)
                    except Exception as exc:
                        update(label, None, str(exc), extra=True)

        try:
            await asyncio.gather(*(one(server) for server in document["sources"]))
        finally:
            await browser.close()


def discover(path, season=1, episode=1, full_scan=False):
    """Start a bounded background scan and return its available choices so far."""
    key = (base_url(), path, season, episode)
    if full_scan:
        key += ("all",)
    now = time.monotonic()
    with _lock:
        entry = _jobs.get(key)
        if entry is None or (
            not entry["pending"]
            and now - entry["updated"] > (_CACHE_TTL if entry["streams"] else 20)
        ):
            if len(_jobs) >= 32:
                completed = [k for k, v in _jobs.items() if not v["pending"]]
                if completed:
                    del _jobs[completed[0]]
                else:
                    raise SourceError("Player discovery is busy. Try again shortly.")
            entry = {
                "pending": True,
                "updated": now,
                "checked": 0,
                "total": None,
                "streams": {},
                "errors": {},
                "queued": True,
                "full_scan": full_scan,
            }
            _jobs[key] = entry
            _workers.submit(_run, key, entry)
        mapping = {}
        for name, stream in entry["streams"].items():
            for label in options(stream):
                mapping.setdefault(label, []).append(name)
        return {
            "providers": mapping,
            "preferred_language": preferred_option(
                mapping, entry.get("original_language", "")
            ),
            "qualities": {
                name: [track["label"] for track in stream.get("qualities", [])]
                for name, stream in entry["streams"].items()
            },
            "discovering": entry["pending"],
            "queued": entry.get("queued", False),
            "checked": entry["checked"],
            "total": entry["total"],
            "error": entry.get("error", ""),
            "scan_mode": "all" if entry.get("full_scan") else "preset",
        }


def _run(key, entry, document=None):
    continued = False
    try:
        origin, path, season, episode = key[:4]
        with _lock:
            entry["queued"] = False
        if document is None:
            document = details(path, season, episode)
            document = {
                **document,
                "sources": ordered_players(
                    document["sources"], entry.get("full_scan", False)
                ),
            }
            with _lock:
                entry["total"] = len(document["sources"])
                entry["original_language"] = document.get("original_language", "")
        if base_url() != origin:
            raise SourceError("The source address changed. Reopen the title.")

        def update(name, stream, error, extra=False):
            with _lock:
                entry["checked"] += 1
                if extra:
                    entry["total"] += 1
                if stream:
                    entry["streams"][name] = stream
                else:
                    entry["errors"][name] = error

        # Yield after two advertised players, rather than monopolizing a worker
        # for the entire 50-player list. No additional parallel requests.
        batch, remaining = document["sources"][:2], document["sources"][2:]
        asyncio.run(_scan({**document, "sources": batch}, update))
        if remaining:
            with _lock:
                entry["queued"] = True
            _workers.submit(
                _run, key, entry, {**document, "sources": remaining}, priority=1
            )
            continued = True
    except Exception as exc:
        with _lock:
            entry["error"] = str(exc)
    finally:
        if not continued:
            with _lock:
                entry["pending"] = False
                entry["queued"] = False
                entry["updated"] = time.monotonic()


def resolve(path, season, episode, provider, language, quality="Best available"):
    """Reprobe only the selected player at download time; signed URLs expire."""
    document = details(path, season, episode)
    base_provider, _, internal = provider.partition(" / ")
    server = next(
        (
            item
            for item in document["sources"]
            if item["name"].casefold() == base_provider.casefold()
        ),
        None,
    )
    if server is None:
        raise SourceError(
            "The selected provider is no longer advertised for this episode."
        )

    if internal:
        server = {**server, "name": provider, "player_server": internal}

    async def run():
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(
                executable_path=browser_executable(runtime), headless=True
            )
            try:
                return await probe(browser, server, timeout=25)
            finally:
                await browser.close()

    stream = asyncio.run(run())
    choices = options(stream)
    if language not in choices:
        # Old queue entries requested the unchanged default source audio.
        if language == "Source Audio":
            choice = {"audio": {"language": "", "uri": ""}, "subtitle": None}
        else:
            raise SourceError(
                "The selected audio/subtitle option is no longer available for this episode."
            )
    else:
        choice = choices[language]
    selected_quality = next(
        (track for track in stream.get("qualities", []) if track["label"] == quality),
        None,
    )
    if quality != "Best available" and selected_quality is None:
        raise SourceError(
            "The selected quality is no longer available for this episode."
        )
    return {
        **stream,
        **choice,
        "quality": selected_quality,
        "title": document["title"],
        "type": document["type"],
    }
