"""Isolated browser access for the English source's visitor confirmation."""

import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from patchright.sync_api import Error, sync_playwright

from ..common.hls import _retry_after
from .source import SourceError, SourceUnavailable, _public_host, details

_lock = threading.Lock()


def browser_executable(runtime):
    configured = os.environ.get("ANIWORLD_123MOVIES_BROWSER_EXECUTABLE", "")
    if configured:
        if not Path(configured).is_file():
            raise SourceError("The configured English-source browser does not exist.")
        return configured
    expected = Path(runtime.chromium.executable_path)
    if expected.is_file():
        return str(expected)
    caches = [
        Path.home() / "Library/Caches/ms-playwright",
        Path.home() / ".cache/ms-playwright",
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",
    ]
    patterns = (
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
        "chromium-*/chrome-win*/chrome.exe",
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    )
    for cache in caches:
        for pattern in patterns:
            candidates = sorted(cache.glob(pattern), reverse=True)
            if candidates:
                return str(candidates[0])
    raise SourceError(
        "Install Chromium with '.venv/bin/python -m patchright install chromium'."
    )


def watch_html(url):
    """Keep the daily visitor cookie; never load the watch-page video or ads."""
    return _visitor_html(url, "#player[data-embed]")


def catalog_html(url):
    """Use the same visitor session for the native catalog filter form."""
    return _visitor_html(url, "#filters")


def _visitor_html(url, selector):
    from ...config import ANIWORLD_CONFIG_DIR

    _public_host(url)
    origin = urlsplit(url).netloc
    profile = ANIWORLD_CONFIG_DIR / "english-browser" / origin
    with _lock, sync_playwright() as runtime:
        context = None
        try:
            context = runtime.chromium.launch_persistent_context(
                str(profile),
                executable_path=browser_executable(runtime),
                headless=True,
            )

            def route_request(route):
                host = urlsplit(route.request.url).netloc
                if host not in (
                    origin,
                    "challenges.cloudflare.com",
                ) or route.request.resource_type in ("image", "media", "font"):
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", route_request)
            page = context.pages[0]
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            if page.title().lower() == "quick check":
                button = page.locator("button").filter(has_text="Continue to")
                button.wait_for(state="visible", timeout=10000)
                button.click(timeout=10000)
            page.locator(selector).wait_for(state="attached", timeout=25000)
            if urlsplit(page.url).netloc != origin:
                raise SourceError(
                    "The source changed domains. Update its saved address."
                )
            return page.content()
        except Error:
            raise SourceError(
                "The source browser could not load the requested page. Verification may need another attempt."
            ) from None
        finally:
            if context is not None:
                context.close()


def usable_playlist(body):
    """Accept quality-selection masters as well as complete episode playlists."""
    return body.lstrip().startswith("#EXTM3U") and (
        "#EXT-X-STREAM-INF:" in body
        or ("#EXTINF:" in body and "#EXT-X-ENDLIST" in body)
    )


def player_http_failure(response):
    """Keep player rate-limit diagnostics and server-requested delays."""
    if response.status not in (429, 503):
        return None
    if response.request.resource_type not in ("document", "fetch", "xhr"):
        return None
    return SourceUnavailable(
        f"{urlsplit(response.url).hostname} returned HTTP {response.status}.",
        retry_after=_retry_after(response.headers.get("Retry-After")) or 0,
    )


def resolve_stream(path, season=1, episode=1):
    """Capture the real main-player HLS response, excluding iframe adverts."""
    document = details(path, season, episode)
    servers = [
        item
        for item in document["sources"]
        if urlsplit(item["url"]).hostname == "vidrock.net"
    ]
    if not servers:
        raise SourceError("This title does not advertise a supported Vidrock server.")
    target = servers[0]["url"]
    _public_host(target)
    with _lock, sync_playwright() as runtime:
        browser = None
        failures = []
        try:
            browser = runtime.chromium.launch(
                executable_path=browser_executable(runtime), headless=True
            )
            page = browser.new_page()
            captured = []
            checked_hosts = set()

            def route_request(route):
                request = route.request
                parts = urlsplit(request.url)
                if (
                    request.resource_type in ("image", "media", "font")
                    or request.frame != page.main_frame
                ):
                    route.abort()
                    return
                if (
                    request.resource_type in ("document", "script")
                    and parts.hostname != "vidrock.net"
                ):
                    route.abort()
                    return
                try:
                    if parts.hostname not in checked_hosts:
                        _public_host(request.url)
                        checked_hosts.add(parts.hostname)
                    route.continue_()
                except SourceError:
                    route.abort()

            def capture(response):
                if response.frame == page.main_frame:
                    failure = player_http_failure(response)
                    if failure:
                        failures.append(failure)
                if (
                    response.status != 200
                    or response.frame != page.main_frame
                    or ".m3u8" not in response.url
                ):
                    return
                try:
                    body = response.text()
                    if usable_playlist(body):
                        headers = {
                            key: value
                            for key, value in response.request.all_headers().items()
                            if key in ("referer", "origin", "user-agent")
                        }
                        captured.append(
                            {
                                "url": response.url,
                                "headers": headers,
                                "title": document["title"],
                                "type": document["type"],
                            }
                        )
                except Error:
                    return

            page.route("**/*", route_request)
            page.on("response", capture)
            page.on("popup", lambda popup: popup.close())
            page.goto(target, wait_until="domcontentloaded", timeout=25000)
            deadline = time.monotonic() + 25
            while not captured and not failures and time.monotonic() < deadline:
                page.wait_for_timeout(200)
            if not captured:
                if failures:
                    raise max(failures, key=lambda error: error.retry_after)
                raise SourceUnavailable(
                    "The video server did not return a usable episode playlist. Try again later."
                )
            return captured[0]
        except Error:
            if failures:
                raise max(failures, key=lambda error: error.retry_after) from None
            raise SourceUnavailable(
                "The video server could not be opened. Try again later."
            ) from None
        finally:
            if browser is not None:
                browser.close()
