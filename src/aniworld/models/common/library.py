"""Reuse existing local series folders across compatible media libraries."""

import re
import unicodedata
from pathlib import Path

_IDS = re.compile(
    r"\s*[\[{](?:imdb|tmdb|tvdb)(?:id)?[-=][\w-]+[\]}]\s*$", re.IGNORECASE
)
_YEARS = re.compile(r"\s*\((?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)\d{2})?\)\s*$")


def _title_key(title):
    title = unicodedata.normalize("NFKC", title)
    previous = None
    while previous != title:
        previous = title
        title = _YEARS.sub("", _IDS.sub("", title))
    return "".join(c for c in title.casefold() if c.isalnum())


def series_folder(base, title):
    """Match a unique title, allowing year/ID suffixes but never fuzzy matches.

    TV and Anime roots alongside each other share series. Other explicit
    destinations search only their own root; Movies is never searched.
    """
    base = Path(base)
    key = _title_key(title)
    if (
        base.name not in ("TV", "Anime", "Movies")
        and base.is_dir()
        and _title_key(base.name) == key
    ):
        return base
    roots = [base]
    if base.name in ("TV", "Anime"):
        roots.append(base.parent / ("Anime" if base.name == "TV" else "TV"))
    matches = []
    for root in roots:
        if not root.is_dir():
            continue
        matches.extend(
            folder
            for folder in root.iterdir()
            if not folder.name.startswith(".")
            and folder.is_dir()
            and not folder.is_symlink()
            and key
            and _title_key(folder.name) == key
        )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple library folders match {title!r}; select its series folder explicitly."
        )
    return matches[0] if matches else base / title


def season_folder(series, number):
    """Keep an existing Season 02/Season 2 convention when adding episodes."""
    series = Path(series)
    matches = []
    if series.is_dir():
        for folder in series.iterdir():
            match = re.fullmatch(r"Season\s+(\d+)", folder.name, re.IGNORECASE)
            if (
                match
                and int(match[1]) == number
                and folder.is_dir()
                and not folder.is_symlink()
            ):
                matches.append(folder)
    if len(matches) > 1:
        raise ValueError(f"Multiple folders match season {number} in {series.name!r}.")
    return matches[0] if matches else series / f"Season {number}"
