"""Configured-origin URL matching and source-specific queue validation."""

from urllib.parse import parse_qs, urlencode, urlsplit

from . import source


def is_source_url(url):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == urlsplit(source.base_url()).netloc
        and bool(source.TITLE_PATH.fullmatch(parsed.path))
        and not parsed.fragment
    )


def selection(url):
    if not is_source_url(url):
        raise source.SourceError("Select a title from the configured 123Movies source.")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    try:
        season = int(query.get("s", [1])[0])
        episode = int(query.get("e", [1])[0])
    except (ValueError, IndexError):
        raise source.SourceError("Invalid season or episode number.") from None
    if not 0 <= season <= 10000 or not 1 <= episode <= 10000:
        raise source.SourceError("Invalid season or episode number.")
    return parsed.path, season, episode


def title_url(path, season=1, episode=1):
    return source.base_url() + path + "?" + urlencode({"s": season, "e": episode})


def providers(url, full_scan=False):
    from .discovery import discover

    return discover(*selection(url), full_scan)


class ConfiguredURLPattern:
    """Match this source against its saved origin at request time."""

    def __init__(self, kind):
        self.kind = kind

    def fullmatch(self, url):
        try:
            if not is_source_url(url):
                return None
            selection(url)
        except source.SourceError:
            return None
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        kind = (
            "episode"
            if "e" in query or parsed.path.startswith("/watch/movie-")
            else "season"
            if "s" in query
            else "series"
        )
        return source.TITLE_PATH.fullmatch(parsed.path) if kind == self.kind else None


def queued_url(entry):
    """Restore both prototype and current queue records against the saved origin."""
    path = entry.get("english_path")
    if entry.get("source") == "movies123":
        path = entry.get("source_path")
    if path is None:
        return None
    if not isinstance(path, str) or not source.TITLE_PATH.fullmatch(path):
        raise source.SourceError("Invalid queued title path.")
    return title_url(path, int(entry["season"]), int(entry["episode"]))


def queue_entries(
    series_url, urls, provider=None, language=None, quality="Best available"
):
    path, _, _ = selection(series_url)
    if provider is not None and (
        not isinstance(provider, str) or not 1 <= len(provider) <= 200
    ):
        raise source.SourceError("Select a valid advertised provider.")
    if not isinstance(urls, list) or not 1 <= len(urls) <= 500:
        raise source.SourceError("Select between 1 and 500 episodes.")
    documents = {}
    entries = []
    seen = set()
    for url in urls:
        selected_path, season, episode = selection(url)
        if selected_path != path:
            raise source.SourceError("All episodes must belong to the selected title.")
        if season not in documents:
            documents[season] = source.details(path, season)
        item = documents[season]
        available = (
            {entry["number"] for entry in item["episodes"]}
            if item["type"] == "tv"
            else {1}
        )
        advertised = {server["name"].casefold() for server in item["sources"]}
        supported = (
            provider.partition(" / ")[0].casefold() in advertised
            if provider
            else item["download_supported"]
        )
        if not supported or episode not in available:
            raise source.SourceError(
                "The selected episode has no supported download server or is unavailable."
            )
        if (season, episode) in seen:
            continue
        seen.add((season, episode))
        if provider and (
            not isinstance(language, str)
            or not 1 <= len(language) <= 200
            or not isinstance(quality, str)
            or not 1 <= len(quality) <= 100
        ):
            raise source.SourceError(
                "Select a valid discovered audio and quality option."
            )
        entries.append(
            {
                "source": "movies123",
                "source_path": path,
                **(
                    {
                        "selected_provider": provider,
                        "selected_language": language,
                        "selected_quality": quality,
                    }
                    if provider
                    else {}
                ),
                "season": season,
                "episode": episode,
                "url": title_url(path, season, episode),
            }
        )
    return documents[next(iter(documents))]["title"], entries
