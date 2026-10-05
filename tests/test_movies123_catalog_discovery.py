"""Native catalog filters, combined discovery, and shared API boundaries."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from aniworld.models.movies123 import catalog_discovery as catalog
from aniworld.models.movies123.source import SourceError
from aniworld.web.views import api_media

FORM = """<form id="filters" action="/browser">
<input id="g1" name="genre[]" value="animation"><label for="g1">Animation</label>
<input id="g2" name="genre[]" value="action-adventure"><label for="g2">Action &amp; Adventure</label>
<input id="c1" name="country[]" value="japan"><label for="c1">Japan</label>
<input id="t1" name="type[]" value="tv"><label for="t1">TV-Shows</label>
<input id="q1" name="quality[]" value="HD"><label for="q1">HD</label>
<input id="y1" name="year[]" value="2026"><label for="y1">2026</label>
<input id="s1" name="sort" value="added_site"><label for="s1">Recently added</label>
<input id="s2" name="sort" value="popularity"><label for="s2">Popular</label>
</form><input id="bad" name="genre[]" value="secret"><label for="bad">Outside</label>"""
LISTING = """<a href="/watch/movie-unfiltered-123">Unfiltered navigation</a>
<div class="bf-grid"><div><a href="/watch/tv-example-123">
<img data-src="https://image.tmdb.org/t/p/w342/image.jpg" alt="Example">
<small>TV · 2026</small></a></div></div>
<a rel="next" href="/browser?genre%5B%5D=animation&amp;page=2">Next</a>"""


@pytest.fixture(autouse=True)
def clear_caches():
    catalog._cache.clear()
    api_media._browse_cache.clear()
    yield
    catalog._cache.clear()
    api_media._browse_cache.clear()


@pytest.fixture
def native_site(monkeypatch):
    calls = []

    def fetch(path, origin):
        calls.append((path, origin))
        return SimpleNamespace(text=FORM if path == "/browser" else LISTING)

    monkeypatch.setattr(catalog, "fetch", fetch)
    return calls


def test_live_advertised_labels_and_group_semantics(native_site):
    groups = {group["key"]: group for group in catalog.filters()}
    assert set(groups) == {"genre", "type", "quality", "country", "year", "sort"}
    assert groups["genre"]["options"] == [
        {"name": "Animation", "slug": "animation"},
        {"name": "Action & Adventure", "slug": "action-adventure"},
    ]
    assert groups["sort"]["options"][0]["slug"] == "added_site"
    assert groups["genre"]["multiple"] and not groups["sort"]["multiple"]
    catalog.filters()
    assert len(native_site) == 1


def test_combined_filters_use_native_query_and_only_filtered_cards(native_site):
    result = catalog.discover(
        {
            "genre": ["animation", "action-adventure"],
            "country": ["japan"],
            "sort": ["added_site"],
        },
        1,
    )
    query = parse_qs(urlsplit(native_site[-1][0]).query)
    assert query == {
        "genre[]": ["animation", "action-adventure"],
        "country[]": ["japan"],
        "sort": ["added_site"],
        "page": ["1"],
    }
    assert [item["title"] for item in result["results"]] == ["Example"]
    assert result["has_more"]
    assert not catalog.discover({"genre": ["animation"]}, 2)["has_more"]


@pytest.mark.parametrize(
    "selected,page",
    [
        ({"genre": ["unknown"]}, 1),
        ({"path": ["/watch/anything"]}, 1),
        ({"sort": ["added_site", "popularity"]}, 1),
        ({"genre": "animation"}, 1),
        ({}, 0),
        ({}, 1001),
    ],
)
def test_invalid_filters_never_fetch_a_listing(native_site, selected, page):
    with pytest.raises(catalog.InvalidFilter):
        catalog.discover(selected, page)
    assert all(path == "/browser" for path, _ in native_site)


def test_metadata_cache_changes_with_configured_domain(native_site, monkeypatch):
    monkeypatch.setattr(catalog, "base_url", lambda: "https://one.example")
    catalog.filters()
    monkeypatch.setattr(catalog, "base_url", lambda: "https://two.example")
    catalog.filters()
    assert [origin for _, origin in native_site] == [
        "https://one.example",
        "https://two.example",
    ]


@pytest.mark.parametrize(
    "href",
    ["https://other.example/browser?page=2", "/browser?page=99", "javascript:alert(1)"],
)
def test_untrusted_next_links_cannot_create_more_pages(href):
    parser = catalog.ListingParser("https://123movie.sx", 1)
    parser.feed(f'<div class="bf-grid"></div><a rel="next" href="{href}">Next</a>')
    assert parser.layout_seen and not parser.has_more


def test_shared_api_contract_and_errors(client, native_site, monkeypatch):
    metadata = client.get("/api/genres?site=movies123").get_json()
    assert len(metadata["filters"]) == 6
    result = client.get("/api/discover?site=movies123&genre=animation&country=japan")
    assert result.status_code == 200
    body = result.get_json()
    assert body["page"] == 1 and body["has_more"]
    assert body["results"][0]["poster_url"]
    assert client.get("/api/discover?site=movies123&genre=nope").status_code == 400
    assert client.get("/api/discover?site=movies123&page=bad").status_code == 400
    assert client.get("/api/discover?site=aniworld").status_code == 400
    assert client.get("/api/genre?site=movies123&slug=animation").status_code == 200

    def unavailable(*args):
        raise SourceError("Source requires verification")

    monkeypatch.setattr(catalog, "discover", unavailable)
    assert (
        client.get("/api/discover?site=movies123&genre=animation&page=3").status_code
        == 502
    )


def test_empty_catalog_is_success_but_changed_layout_is_failure(
    native_site, monkeypatch
):
    catalog.filters()
    monkeypatch.setattr(
        catalog,
        "fetch",
        lambda *args: SimpleNamespace(text='<div class="bf-grid"></div>'),
    )
    assert catalog.discover({"genre": ["animation"]}) == {
        "results": [],
        "has_more": False,
    }
    monkeypatch.setattr(
        catalog,
        "fetch",
        lambda *args: SimpleNamespace(
            text='<section><div class="text-center">No items match those filters.</div></section>'
        ),
    )
    assert catalog.discover({"genre": ["animation"]}) == {
        "results": [],
        "has_more": False,
    }
    monkeypatch.setattr(
        catalog,
        "fetch",
        lambda *args: SimpleNamespace(text="<html>Changed layout</html>"),
    )
    with pytest.raises(SourceError, match="unsupported"):
        catalog.discover({"genre": ["animation"]})


def test_single_category_uses_native_path_and_native_pagination(
    native_site, monkeypatch
):
    catalog.filters()
    body = LISTING.replace(
        "/browser?genre%5B%5D=animation&amp;page=2", "/genre/animation?page=2"
    )
    monkeypatch.setattr(
        catalog,
        "fetch",
        lambda path, origin: (
            native_site.append((path, origin)) or SimpleNamespace(text=body)
        ),
    )
    assert catalog.discover({"genre": ["animation"]})["has_more"]
    assert native_site[-1][0] == "/genre/animation"
    assert not catalog.discover({"genre": ["animation"]}, 2)["has_more"]
    assert native_site[-1][0] == "/genre/animation?page=2"


def test_browser_session_only_used_for_visitor_verification(native_site, monkeypatch):
    from aniworld.models.movies123 import browser

    catalog.filters()
    calls = []

    def verification(*args):
        raise SourceError("The source requires browser verification.")

    monkeypatch.setattr(catalog, "fetch", verification)
    monkeypatch.setattr(
        browser, "catalog_html", lambda url: calls.append(url) or LISTING
    )
    assert catalog.discover({"genre": ["animation"], "type": ["tv"]})["results"]
    assert "genre%5B%5D=animation" in calls[0]
    calls.clear()

    def failed(*args):
        raise SourceError("The source did not respond successfully.")

    monkeypatch.setattr(catalog, "fetch", failed)
    with pytest.raises(SourceError, match="respond"):
        catalog.discover({"genre": ["animation"]})
    assert not calls
