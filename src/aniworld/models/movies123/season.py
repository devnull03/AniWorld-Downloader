"""Fetch episode metadata only when a season is opened or downloaded."""

from ..common import run_each
from . import source
from .urls import selection, title_url


class Movies123Season:
    def __init__(self, url, series=None):
        self.path, self.season_number, _ = selection(url)
        self.url = title_url(self.path, self.season_number)
        self.series = series
        self.are_movies = self.path.startswith("/watch/movie-")
        self._episodes = None

    @property
    def episodes(self):
        from .episode import Movies123Episode

        if self._episodes is None:
            if self.series is not None and self.series._season == self.season_number:
                item = self.series.document
            else:
                item = source.details(self.path, self.season_number)
            entries = (
                item["episodes"]
                if item["type"] == "tv"
                else [{"number": 1, "title": item["title"]}]
            )
            self._episodes = [
                Movies123Episode(
                    url=title_url(self.path, self.season_number, entry["number"]),
                    season=self,
                    metadata=entry,
                )
                for entry in entries
            ]
        return self._episodes

    @property
    def episode_count(self):
        return len(self.episodes)

    def download(self):
        run_each(self.episodes, "download")
