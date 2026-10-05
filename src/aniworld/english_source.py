"""123Movies catalog and advertised episode players.

Keep title identifiers relative to the configured origin so changing a domain
does not change a title's identity. Remote scripts are never executed here.
"""

import html
import ipaddress
import json
import os
import re
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import niquests

DEFAULT_BASE_URL = "https://123movie.sx"
ENV_KEY = "ANIWORLD_123MOVIES_BASE_URL"
TITLE_PATH = re.compile(r"/watch/(tv|movie)-[a-zA-Z0-9-]+$")
MAX_BODY = 2 * 1024 * 1024


class SourceError(ValueError):
    """The source is invalid, unavailable, or returned an unsupported page."""


class SourceUnavailable(SourceError):
    """A player failed transiently and may be retried with a fresh session."""

    def __init__(self, message, retry_after=0, player_servers=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.player_servers = player_servers or []


def normalize_base_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value.strip()):
        raise SourceError("Enter an HTTPS website address without spaces.")
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError):
        raise SourceError("Invalid website address.") from None
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "." not in host
        or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
        or host.endswith((".local", ".localhost", ".internal", ".test", ".invalid"))
    ):
        raise SourceError("Use a public HTTPS domain with no path, port, or login.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"https://{host}"
    raise SourceError("Use a website domain, not an IP address.")


def base_url():
    return normalize_base_url(os.environ.get(ENV_KEY, DEFAULT_BASE_URL))


def _public_host(url):
    host = urlsplit(url).hostname
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise SourceError(
            "Cannot resolve the source domain. Check its current address."
        ) from None
    if not addresses or any(
        not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses
    ):
        raise SourceError("The source must resolve to public internet addresses.")


def fetch(path, origin=None):
    """Bound requests and redirects; report verification pages as failures."""
    origin = normalize_base_url(origin or base_url())
    target = origin + path
    with niquests.Session() as session:
        session.trust_env = False
        for _ in range(4):
            _public_host(target)
            try:
                response = session.get(
                    target,
                    timeout=15,
                    allow_redirects=False,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "text/html,application/json",
                    },
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    redirected = urljoin(target, response.headers.get("Location", ""))
                    if urlsplit(redirected).netloc != urlsplit(origin).netloc:
                        raise SourceError(
                            "The source redirects to a different domain. Check and save its new address."
                        )
                    if urlsplit(redirected).scheme != "https":
                        raise SourceError("The source redirected away from HTTPS.")
                    target = redirected
                    continue
                body = response.text
                if len(body.encode("utf-8")) > MAX_BODY:
                    raise SourceError("The source response is too large.")
                if re.search(
                    r"<title[^>]*>\s*(Quick check|Just a moment)", body, re.IGNORECASE
                ):
                    raise SourceError(
                        "The source requires browser verification. Catalog or downloads cannot be fetched yet."
                    )
                response.raise_for_status()
                return response
            except niquests.exceptions.RequestException:
                raise SourceError(
                    "The source did not respond successfully. Check its address or try again."
                ) from None
    raise SourceError("The source returned too many redirects.")


def _title_path(url, origin):
    try:
        parts = urlsplit(urljoin(origin + "/", url))
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc != urlsplit(origin).netloc:
        return None
    return parts.path if TITLE_PATH.fullmatch(parts.path) else None


def _poster(value, origin):
    if not isinstance(value, str):
        return ""
    value = urljoin(origin + "/", value) if value else ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    # No arbitrary third-party images in the browser or the app's image proxy.
    if parts.scheme == "https" and parts.netloc in (
        urlsplit(origin).netloc,
        "image.tmdb.org",
    ):
        return value
    return ""


def _card(title, path, origin, poster="", year=""):
    return {
        "title": html.unescape(str(title)).strip(),
        "path": path,
        "url": origin + path,
        "type": "tv" if path.startswith("/watch/tv-") else "movie",
        "poster": _poster(poster, origin),
        "year": str(year or ""),
        "download_supported": None,  # Checked when the title's player is opened.
    }


class CatalogParser(HTMLParser):
    """Parse observed homepage title links without executing any page scripts."""

    def __init__(self, origin):
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.items = {}
        self.current = None
        self.text = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            path = _title_path(attrs.get("href", ""), self.origin)
            self.current = (
                _card(attrs.get("title", ""), path, self.origin) if path else None
            )
            self.text = []
        elif tag == "small":
            self.skip += 1
        elif tag == "img" and self.current:
            self.current["poster"] = next(
                (
                    poster
                    for key in ("data-lsrc", "data-src", "src")
                    if (poster := _poster(attrs.get(key, ""), self.origin))
                ),
                "",
            )
            if not self.current["title"]:
                self.current["title"] = attrs.get("alt", "")

    def handle_data(self, data):
        if self.current and not self.skip:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "small":
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.current:
            self.current["title"] = self.current["title"] or " ".join(
                " ".join(self.text).split()
            )
            if self.current["title"]:
                previous = self.items.get(self.current["path"])
                if previous is None or (
                    self.current["poster"] and not previous["poster"]
                ):
                    self.items[self.current["path"]] = self.current
            self.current = None


def catalog(keyword=""):
    origin = base_url()
    if keyword:
        response = fetch("/api/suggest?" + urlencode({"q": keyword}), origin)
        try:
            raw = response.json()
        except ValueError:
            raise SourceError(
                "The source returned an unsupported search response."
            ) from None
        if not isinstance(raw, list):
            raise SourceError("The source returned an unsupported search response.")
        items = {}
        for item in raw[:100]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            path = _title_path(item["url"], origin)
            if path and item.get("title"):
                items[path] = _card(
                    item["title"],
                    path,
                    origin,
                    item.get("poster", ""),
                    item.get("year", ""),
                )
        return list(items.values())
    response = fetch("/home", origin)
    parser = CatalogParser(origin)
    parser.feed(response.text)
    if not parser.items:
        raise SourceError("No catalog titles found. The site layout may have changed.")
    items = list(parser.items.values())
    # Hero buttons also link to titles, but the poster cards carry their names
    # and artwork. Prefer those when the page has them.
    cards = [item for item in items if item["poster"]]
    return (cards or items)[:60]


def check_address(value):
    origin = normalize_base_url(value)
    response = fetch("/", origin)
    parser = CatalogParser(origin)
    parser.feed(response.text)
    if (
        not any(brand in response.text.lower() for brand in ("123movies", "gomovies"))
        or not parser.items
    ):
        raise SourceError(
            "This address does not have the expected 123Movies/GoMovies catalog layout."
        )
    return {"base_url": origin, "catalog_available": True, "download_supported": None}


class WatchParser(HTMLParser):
    """Observed watch-page metadata, season links, episode links and servers."""

    def __init__(self, body, origin, path):
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.path = path
        self.seasons = {}
        self.episodes = {}
        self.metadata = {}
        self.servers = {}
        self.scripts = []
        self.script_type = None
        self.script_text = []
        self.button = None
        self.button_text = []
        self.feed(body)
        match = re.search(r"window\.__OPT\s*=\s*", body)
        self.sources = []
        if match:
            try:
                options, _ = json.JSONDecoder().raw_decode(body[match.end() :])
            except ValueError:
                options = []
            if isinstance(options, list):
                for index, url in enumerate(options):
                    if not isinstance(url, str) or urlsplit(url).scheme != "https":
                        continue
                    self.sources.append(
                        {
                            "name": self.servers.get(index, urlsplit(url).hostname),
                            "url": url,
                        }
                    )

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "script":
            self.script_type = attrs.get("type", "")
            self.script_text = []
        if tag == "button" and "opt-pill" in classes:
            try:
                self.button = int(attrs.get("data-i", ""))
            except ValueError:
                self.button = None
            self.button_text = []
        if tag != "a":
            return
        url = attrs.get("href", "")
        if _title_path(url, self.origin) != self.path:
            return
        query = parse_qs(urlsplit(url).query)
        try:
            season = int(query.get("s", [1])[0])
            episode = int(query.get("e", [1])[0])
        except ValueError:
            return
        if not 0 <= season <= 10000 or not 1 <= episode <= 10000:
            return
        if "season-pill" in classes:
            self.seasons[season] = {"number": season, "title": f"Season {season}"}
        if "ep-card" in classes or "ep-tile" in classes:
            title = attrs.get("title", f"Episode {episode}")
            title = re.sub(r"^E\d+\s*[·:-]\s*", "", title)
            trailer_only = bool(
                re.search(r"\s*\(trailer only\)\s*$", title, re.IGNORECASE)
            )
            title = re.sub(r"\s*\(trailer only\)\s*$", "", title, flags=re.IGNORECASE)
            self.episodes[episode] = {
                "number": episode,
                "season": season,
                "title": title,
                "path": self.path,
                "availability_hint": "trailer_only" if trailer_only else "",
            }

    def handle_data(self, data):
        if self.script_type is not None:
            self.script_text.append(data)
        if self.button is not None:
            self.button_text.append(data)

    def handle_endtag(self, tag):
        if tag == "button" and self.button is not None:
            self.servers[self.button] = "".join(self.button_text).strip()
            self.button = None
        if tag == "script" and self.script_type is not None:
            if self.script_type == "application/ld+json":
                try:
                    item = json.loads("".join(self.script_text))
                    if isinstance(item, dict) and item.get("@type") in (
                        "TVSeries",
                        "Movie",
                    ):
                        self.metadata = item
                except ValueError:
                    pass
            self.script_type = None


def details(path, season=1, episode=1):
    if not isinstance(path, str) or not TITLE_PATH.fullmatch(path):
        raise SourceError("Select a valid movie or show from the catalog.")
    if not 0 <= season <= 10000 or not 1 <= episode <= 10000:
        raise SourceError("Invalid season or episode number.")
    origin = base_url()
    request_path = path + "?" + urlencode({"s": season, "e": episode})
    try:
        body = fetch(request_path, origin).text
    except SourceError as exc:
        if "browser verification" not in str(exc):
            raise
        from .english_browser import watch_html

        body = watch_html(origin + request_path)
    parsed = WatchParser(body, origin, path)
    if not parsed.metadata or not parsed.sources:
        raise SourceError(
            "The source watch-page layout is unsupported or the player is unavailable."
        )
    return {
        "title": parsed.metadata.get("name", "Unknown"),
        "path": path,
        "url": origin + request_path,
        "description": parsed.metadata.get("description", ""),
        "original_language": parsed.metadata.get("inLanguage", ""),
        "type": "tv" if path.startswith("/watch/tv-") else "movie",
        "seasons": list(parsed.seasons.values()),
        "episodes": list(parsed.episodes.values()),
        "sources": parsed.sources,
        "download_supported": any(
            urlsplit(item["url"]).hostname == "vidrock.net" for item in parsed.sources
        ),
    }
