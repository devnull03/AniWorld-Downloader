"""Catalog parsing, domain changes, persistence, and prototype boundaries."""

import json
import socket
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv

from aniworld import english_source as source
from aniworld.web import db, settings_store

# Sanitized shapes observed on 123movie.sx, not a simulated player API.
CATALOG = """<title>123Movies</title>
<a href="/watch/tv-reacher-2pzovm4q"><span>Reacher</span><small>TV · 2022</small></a>
<a href="/watch/movie-jack-reacher-3uhcar44">Jack Reacher</a>
<a href="/watch/tv-reacher-2pzovm4q">Reacher again</a>
<a href="https://other.example/watch/tv-unrelated-123">Other</a>
<a href="javascript:alert(1)">Bad</a>"""
SEARCH = [
    {
        "url": "/watch/tv-reacher-2pzovm4q",
        "title": "Reacher",
        "type": "tv",
        "year": "2022",
        "poster": "https://image.tmdb.org/t/p/w92/poster.jpg",
    }
]


def response(body=CATALOG, status=200, headers=None):
    return SimpleNamespace(
        text=body,
        status_code=status,
        headers=headers or {},
        json=lambda: json.loads(body),
        raise_for_status=lambda: None,
    )


def test_homepage_parser_deduplicates_titles_and_rejects_foreign_links():
    parser = source.CatalogParser(source.DEFAULT_BASE_URL)
    parser.feed(CATALOG)
    assert [item["title"] for item in parser.items.values()] == [
        "Reacher",
        "Jack Reacher",
    ]
    assert all(item["download_supported"] is None for item in parser.items.values())


def test_changing_domain_keeps_title_paths(monkeypatch):
    monkeypatch.setattr(source, "fetch", lambda *args: response(json.dumps(SEARCH)))
    original = source.catalog("reacher")[0]
    monkeypatch.setenv(source.ENV_KEY, "https://new-domain.example")
    migrated = source.catalog("reacher")[0]
    assert migrated["path"] == original["path"]
    assert migrated["url"] == "https://new-domain.example" + original["path"]


def test_search_uses_observed_endpoint_and_encodes_keyword(monkeypatch):
    calls = []

    def fetch(path, origin):
        calls.append((path, origin))
        return response(json.dumps(SEARCH))

    monkeypatch.setattr(source, "fetch", fetch)
    assert source.catalog("A&B")[0]["year"] == "2022"
    assert calls == [("/api/suggest?q=A%26B", source.DEFAULT_BASE_URL)]


def test_unexpected_search_layout_is_an_error(monkeypatch):
    monkeypatch.setattr(source, "fetch", lambda *args: response("{}"))
    with pytest.raises(source.SourceError, match="unsupported search"):
        source.catalog("test")


@pytest.mark.parametrize(
    "value",
    [
        "http://123movie.sx",
        "https://123movie.sx/watch/title",
        "https://user:pw@123movie.sx",
        "https://127.0.0.1",
        "https://[::1]",
        "https://server.local",
        "https://example.com:8080",
        "https://example.com?key=secret",
        "https://example.com#fragment",
        "",
        None,
        "https://123movie.sx\nANIWORLD_WEB_AUTH=0",
    ],
)
def test_invalid_origin_is_rejected(value):
    with pytest.raises(source.SourceError):
        source.normalize_base_url(value)


def test_origin_normalization():
    assert (
        source.normalize_base_url(" https://123MOVIE.sx/ ") == source.DEFAULT_BASE_URL
    )


def test_url_setting_survives_restart_and_preserves_other_settings(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings_store, "ANIWORLD_CONFIG_DIR", tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("# retained\nANIWORLD_UI_LANGUAGE=de\n")
    settings_store.update_settings(
        {"movies123_base_url": "https://new-domain.example/"}
    )
    monkeypatch.delenv(source.ENV_KEY)
    load_dotenv(env_file)
    assert source.base_url() == "https://new-domain.example"
    assert "# retained\nANIWORLD_UI_LANGUAGE=de\n" in env_file.read_text()


def test_failed_persistence_does_not_apply_settings(monkeypatch):
    from aniworld import env

    def fail(*args):
        raise OSError("read-only")

    monkeypatch.setattr(env, "persist_env_values", fail)
    with pytest.raises(settings_store.SettingsError, match="Could not save"):
        settings_store.update_settings(
            {"movies123_base_url": "https://new-domain.example", "ui_language": "de"}
        )
    assert source.base_url() == source.DEFAULT_BASE_URL
    assert settings_store.ui_language() == "en"


def test_invalid_settings_batch_changes_nothing(client):
    result = client.put(
        "/api/settings",
        json={"movies123_base_url": "http://bad.example", "ui_language": "de"},
    )
    assert result.status_code == 400
    assert settings_store.ui_language() == "en"


def test_check_does_not_change_saved_address(monkeypatch, client):
    monkeypatch.setattr(source, "fetch", lambda *args: response())
    result = client.post(
        "/api/english/check", json={"base_url": "https://new-domain.example"}
    )
    assert result.status_code == 200
    assert result.get_json()["download_supported"] is None
    assert source.base_url() == source.DEFAULT_BASE_URL


def test_address_check_rejects_unrelated_site(monkeypatch):
    monkeypatch.setattr(
        source, "fetch", lambda *args: response("<title>Parked domain</title>")
    )
    with pytest.raises(source.SourceError, match="expected 123Movies"):
        source.check_address(source.DEFAULT_BASE_URL)


def test_catalog_defers_download_capability_until_title_selection(monkeypatch, client):
    monkeypatch.setattr(source, "fetch", lambda *args: response())
    body = client.get("/api/english/catalog").get_json()
    assert len(body["results"]) == 2
    assert body["download_supported"] is None
    result = client.get("/english")
    assert result.status_code == 302
    assert result.location == "/?site=movies123"
    page = client.get(result.location).get_data(as_text=True)
    assert 'data-site="movies123"' in page
    assert "English (prototype)" not in page
    assert db.get_next_queued() is None


@pytest.mark.parametrize("scope", ["read", "write"])
def test_address_check_requires_admin_key(client, api_key, scope):
    raw, _ = api_key(scope=scope)
    result = client.post(
        "/api/english/check",
        json={"base_url": source.DEFAULT_BASE_URL},
        headers={"X-API-Key": raw},
    )
    assert result.status_code == 403


def test_catalog_requires_login(auth_client):
    db.create_user("root", "examplepassword", role="admin")
    assert auth_client.get("/english").status_code == 302
    assert auth_client.get("/api/english/catalog").status_code in (302, 401)


def test_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(source.SourceError, match="public internet"):
        source.fetch("/")


def mock_session(monkeypatch, result):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return result

    monkeypatch.setattr(source.niquests, "Session", Session)
    monkeypatch.setattr(source, "_public_host", lambda url: None)


def test_verification_page_is_not_an_empty_success(monkeypatch, client):
    mock_session(monkeypatch, response("<title>Quick check</title>"))
    result = client.get("/api/english/catalog")
    assert result.status_code == 502
    assert "browser verification" in result.get_json()["error"]


def test_cross_domain_redirect_requires_manual_update(monkeypatch):
    mock_session(
        monkeypatch,
        response(status=302, headers={"Location": "https://new-domain.example/"}),
    )
    with pytest.raises(source.SourceError, match="different domain"):
        source.fetch("/")
    assert source.base_url() == source.DEFAULT_BASE_URL


def test_private_redirect_is_not_followed(monkeypatch):
    mock_session(
        monkeypatch, response(status=302, headers={"Location": "https://127.0.0.1/"})
    )
    with pytest.raises(source.SourceError, match="different domain"):
        source.fetch("/")


def test_browse_uses_image_cards_and_preserves_titles_and_lazy_posters(monkeypatch):
    body = """<a href="/watch/tv-reacher-2pzovm4q">Watch Now</a>
    <a class="bf-card" href="/watch/tv-reacher-2pzovm4q" title="Reacher">
      <img src="data:image/gif;base64,placeholder" data-lsrc="https://image.tmdb.org/t/p/w342/reacher.jpg" alt="Reacher">
      <span>Eps 8</span><div>Reacher</div></a>
    <a class="bf-card" href="/watch/movie-jack-reacher-3uhcar44">
      <img src="https://image.tmdb.org/t/p/w342/jack.jpg" alt="Jack Reacher"><small>2022</small></a>"""
    calls = []
    monkeypatch.setattr(
        source, "fetch", lambda path, origin: calls.append(path) or response(body)
    )
    items = source.catalog()
    assert calls == ["/home"]
    assert [item["title"] for item in items] == ["Reacher", "Jack Reacher"]
    assert [item["poster"] for item in items] == [
        "https://image.tmdb.org/t/p/w342/reacher.jpg",
        "https://image.tmdb.org/t/p/w342/jack.jpg",
    ]
