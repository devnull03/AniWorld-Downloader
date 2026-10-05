"""Discover titles using the site's advertised catalog filters."""

import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .source import CatalogParser, SourceError, base_url, fetch


class InvalidFilter(ValueError):
    """A client selected a filter that this source does not advertise."""


GROUPS = {
    "genre": "Genre",
    "type": "Type",
    "country": "Country",
    "year": "Released",
    "quality": "Quality",
    "sort": "Sort",
}
_cache = {}
_lock = threading.Lock()


class FilterParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_form = False
        self.inputs = {}
        self.labels = {}
        self.label = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.in_form = attrs.get("id") == "filters"
        if not self.in_form:
            return
        if tag == "input":
            group = attrs.get("name", "").removesuffix("[]")
            value = attrs.get("value", "")
            if group in GROUPS and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                self.inputs[attrs.get("id")] = (group, value)
        elif tag == "label":
            self.label = attrs.get("for")
            self.text = []

    def handle_data(self, data):
        if self.label:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "label" and self.label:
            self.labels[self.label] = " ".join(" ".join(self.text).split())
            self.label = None
        elif tag == "form":
            self.in_form = False

    def groups(self):
        groups = []
        for key, label in GROUPS.items():
            options = {
                value: self.labels[identifier]
                for identifier, (group, value) in self.inputs.items()
                if group == key and self.labels.get(identifier)
            }
            if options:
                groups.append(
                    {
                        "key": key,
                        "name": label,
                        "multiple": key != "sort",
                        "options": [
                            {"name": name, "slug": value}
                            for value, name in options.items()
                        ],
                    }
                )
        return groups


def filters():
    origin = base_url()
    with _lock:
        entry = _cache.get(origin)
        if entry and time.monotonic() - entry[0] < 300:
            return entry[1]
    parser = FilterParser()
    parser.feed(fetch("/browser", origin).text)
    groups = parser.groups()
    if not any(group["key"] == "genre" for group in groups):
        raise SourceError("The source did not advertise catalog filters.")
    with _lock:
        _cache[origin] = (time.monotonic(), groups)
    return groups


def genres():
    return next(group["options"] for group in filters() if group["key"] == "genre")


class ListingParser(CatalogParser):
    def __init__(self, origin, page, listing_path="/browser"):
        super().__init__(origin)
        self.listing_path = listing_path
        self.page = page
        self.depth = 0
        self.layout_seen = False
        self.empty_seen = False
        self.has_more = False

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "a" and "next" in data.get("rel", "").split():
            url = urlsplit(urljoin(self.origin, data.get("href", "")))
            if (
                url.scheme == "https"
                and url.netloc == urlsplit(self.origin).netloc
                and url.path == self.listing_path
                and parse_qs(url.query).get("page") == [str(self.page + 1)]
            ):
                self.has_more = True
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "bf-grid" in data.get("class", "").split():
                self.depth = 1
                self.layout_seen = True
        if self.depth:
            super().handle_starttag(tag, attrs)

    def handle_data(self, data):
        if data.strip() == "No items match those filters.":
            self.empty_seen = True
        if self.depth:
            super().handle_data(data)

    def handle_endtag(self, tag):
        if self.depth:
            super().handle_endtag(tag)
            if tag == "div":
                self.depth -= 1


def discover(selected, page=1):
    if type(page) is not int or not 1 <= page <= 1000:
        raise InvalidFilter("Page must be between 1 and 1000.")
    advertised = {group["key"]: group for group in filters()}
    params = []
    for key, values in selected.items():
        group = advertised.get(key)
        if group is None or not isinstance(values, (list, tuple)):
            raise InvalidFilter("Invalid discovery filter.")
        allowed = {option["slug"] for option in group["options"]}
        if len(values) > len(allowed) or (not group["multiple"] and len(values) > 1):
            raise InvalidFilter("Too many filter values.")
        if any(value not in allowed for value in values):
            raise InvalidFilter("The selected tag is not advertised by this source.")
        params.extend(
            (key + "[]" if group["multiple"] else key, value)
            for value in dict.fromkeys(values)
        )
    # Native category routes avoid the visitor check on browser query URLs.
    listing_path = "/browser"
    if len(selected) == 1:
        key, values = next(iter(selected.items()))
        if key in ("genre", "country") and len(values) == 1:
            listing_path = f"/{key}/{values[0]}"
            params = []
    if listing_path == "/browser" or page > 1:
        params.append(("page", str(page)))
    origin = base_url()
    path = listing_path + ("?" + urlencode(params) if params else "")
    try:
        body = fetch(path, origin).text
    except SourceError as exc:
        if "browser verification" not in str(exc).lower():
            raise
        from .browser import catalog_html

        body = catalog_html(origin + path)
    parser = ListingParser(origin, page, listing_path)
    parser.feed(body)
    if not parser.layout_seen and not parser.empty_seen:
        raise SourceError("The source returned an unsupported catalog listing.")
    return {"results": list(parser.items.values()), "has_more": parser.has_more}
