"""Adapt 123Movies to the shared search, title modal, and queue contracts."""

from urllib.parse import parse_qs, urlencode, urlsplit

from .. import english_source as source


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


def document(url):
    return source.details(*selection(url))


def title_url(path, season=1, episode=1):
    return source.base_url() + path + "?" + urlencode({"s": season, "e": episode})


def series(url):
    item = document(url)
    return {
        "title": item["title"],
        "description": item["description"]
        + "\nAudio is preserved from the source; languages are not independently verified.",
        "poster_url": "",
        "genres": [],
        "release_year": "",
        "media_type": item["type"],
    }


def seasons(url):
    item = document(url)
    entries = item["seasons"] if item["type"] == "tv" else [{"number": 1}]
    return [
        {
            "url": title_url(item["path"], entry["number"]),
            "season_number": entry["number"],
            "episode_count": None if item["type"] == "tv" else 1,
            "are_movies": item["type"] == "movie",
        }
        for entry in entries
    ]


def episodes(url):
    item = document(url)
    _, season, _ = selection(url)
    entries = (
        item["episodes"]
        if item["type"] == "tv"
        else [{"number": 1, "title": item["title"]}]
    )
    return [
        {
            "url": title_url(item["path"], season, entry["number"]),
            "episode_number": entry["number"],
            "title_en": entry["title"],
            "title_de": "",
            "downloaded": False,
            "availability_hint": entry.get("availability_hint", ""),
            "available_languages": ["Source Audio"]
            if item["download_supported"]
            else [],
        }
        for entry in entries
    ]


def providers(url):
    from ..english_discovery import discover

    return discover(*selection(url))


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
                "english_path": path,
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
                "url": f"{path}?s={season}&e={episode}",
            }
        )
    return documents[next(iter(documents))]["title"], entries
