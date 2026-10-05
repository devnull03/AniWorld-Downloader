"""Lazy title metadata and season enumeration."""

from ..common import run_each
from ..common.common import clean_title
from . import source
from .urls import selection, title_url


class Movies123Series:
    def __init__(self, url):
        self.path, self._season, self._episode = selection(url)
        self.url = source.base_url() + self.path
        self._document = None
        self._seasons = None

    @property
    def document(self):
        if self._document is None:
            self._document = source.details(self.path, self._season, self._episode)
        return self._document

    @property
    def title(self):
        return self.document["title"]

    @property
    def title_cleaned(self):
        return clean_title(self.title)

    @property
    def description(self):
        return self.document["description"]

    @property
    def media_type(self):
        return self.document["type"]

    @property
    def seasons(self):
        from .season import Movies123Season

        if self._seasons is None:
            entries = (
                self.document["seasons"] if self.media_type == "tv" else [{"number": 1}]
            )
            self._seasons = [
                Movies123Season(title_url(self.path, entry["number"]), series=self)
                for entry in entries
            ]
        return self._seasons

    def download(self):
        run_each(self.seasons, "download")
