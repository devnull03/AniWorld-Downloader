"""Download observed English-source HLS streams through the existing queue."""

import json
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path

import ffmpeg
import niquests

from ...config import logger
from ..common.common import _run_ffmpeg_with_progress, clean_title
from ..common.hls import cleanup_temp_files, download_hls_parallel
from .browser import resolve_stream
from .source import TITLE_PATH, SourceError, SourceUnavailable, _public_host
from .urls import selection, title_url


def normalize_segment(data):
    """Some observed segments are MPEG-TS with a 70-byte PNG prefix.

    Remove only a short image prefix followed by five aligned TS sync bytes.
    Plain MPEG-TS and fMP4 are unchanged; arbitrary images fail explicitly.
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data
    for offset in range(8, min(4096, len(data) - 4 * 188)):
        if all(data[offset + packet * 188] == 0x47 for packet in range(5)):
            return data[offset:]
    raise SourceError("The media segment is an image without recognizable video data.")


def verify_media(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    metadata = json.loads(result.stdout)
    streams = metadata.get("streams", [])
    video = any(
        item.get("codec_type") == "video"
        and item.get("width", 0) > 0
        and item.get("height", 0) > 0
        for item in streams
    )
    audio = any(item.get("codec_type") == "audio" for item in streams)
    if (
        not video
        or not audio
        or float(metadata.get("format", {}).get("duration", 0)) <= 0
    ):
        raise SourceError("Downloaded media did not contain usable video and audio.")
    return metadata


class _EpisodeDownload:
    def __init__(
        self,
        path,
        season,
        episode,
        selected_path,
        queue_id=None,
        selected_provider=None,
        selected_language="Source Audio",
        selected_quality="Best available",
    ):
        if not isinstance(path, str) or not TITLE_PATH.fullmatch(path):
            raise SourceError("Invalid source title path.")
        self.path = path
        self.season_number = int(season)
        self.episode = int(episode)
        self.use_default_path = not selected_path
        self.selected_path = Path(
            selected_path
            or os.environ.get("ANIWORLD_DOWNLOAD_PATH")
            or Path.home() / "Downloads"
        )
        self.queue_id = queue_id
        self.selected_provider = selected_provider
        self.selected_language = selected_language
        self.selected_quality = selected_quality

    def _check_cancelled(self):
        if self.queue_id is not None:
            from ...web.db import is_queue_force_cancelled

            if is_queue_force_cancelled(self.queue_id):
                raise SourceError("Download cancelled.")

    def _resolve_once(self):
        if self.selected_provider:
            from .discovery import resolve

            return resolve(
                self.path,
                self.season_number,
                self.episode,
                self.selected_provider,
                self.selected_language,
                self.selected_quality,
            )
        return resolve_stream(self.path, self.season_number, self.episode)

    def _resolve_with_retry(self):
        # Like VOE's extractor, retry opening a transiently unavailable player.
        # Each attempt obtains a fresh playlist; explicit choices stay intact.
        for attempt in range(3):
            self._check_cancelled()
            try:
                return self._resolve_once()
            except SourceUnavailable as error:
                if attempt == 2 or error.retry_after > 60:
                    raise
                logger.warning(
                    "[English source] Player unavailable; retrying with backoff"
                )
                delay = max(
                    error.retry_after, 2 ** (attempt + 1) + random.uniform(0, 1)
                )
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    self._check_cancelled()
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def download(self, sample_segments=None):
        stream = self._resolve_with_retry()
        title = clean_title(stream["title"])
        if not title or title in (".", ".."):
            raise SourceError("Invalid source title.")
        base = self.selected_path
        # With a conventional Jellyfin Movies/TV library root, route default
        # downloads by media type. Explicitly selected folders still win.
        if self.use_default_path and base.name in ("Movies", "TV"):
            sibling = base.parent / ("TV" if stream["type"] == "tv" else "Movies")
            if sibling.is_dir():
                base = sibling
        folder = base / title
        filename = title
        if stream["type"] == "tv":
            folder = folder / f"Season {self.season_number}"
            filename = f"{title} S{self.season_number:02d}E{self.episode:02d}"
        if self.selected_provider:
            qualifier = clean_title(
                f"{self.selected_provider} {self.selected_language} {self.selected_quality}"
            )[:100]
            filename += f" [{qualifier}]"
        if sample_segments is not None:
            filename += ".sample"
        output = folder / (filename + ".mkv")
        if output.is_file():
            verify_media(output)
            return output
        folder.mkdir(parents=True, exist_ok=True)
        staging = folder / ".download-staging"
        staging.mkdir(exist_ok=True)
        workspace = tempfile.TemporaryDirectory(prefix="episode-", dir=staging)
        temporary = Path(workspace.name) / "video.pending.mkv"
        prefix = Path(workspace.name) / "stream.source"
        caption_input = Path(workspace.name) / "captions.vtt"
        caption_pending = Path(workspace.name) / "captions.eng.pending.srt"
        caption_output = output.with_suffix(".eng.srt")

        def transform(data):
            self._check_cancelled()
            return normalize_segment(data)

        try:
            files = download_hls_parallel(
                stream["url"],
                prefix,
                headers=stream["headers"],
                preferred_audio_lang=stream.get("audio", {}).get("language", ""),
                audio_rendition_uri=stream.get("audio", {}).get("uri") or None,
                video_variant_uri=(stream.get("quality") or {}).get("uri"),
                label=filename,
                segment_transform=transform,
                segment_limit=sample_segments,
                concurrency_limit=3,
            )
            inputs = [ffmpeg.input(str(path)) for path in files]
            node = ffmpeg.output(*inputs, str(temporary), c="copy")
            _run_ffmpeg_with_progress(node, label=filename)
            verify_media(temporary)
            if stream.get("subtitle"):
                caption_input.write_bytes(
                    fetch_subtitle(stream["subtitle"]["url"], stream["headers"])
                )
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(caption_input),
                        str(caption_pending),
                    ],
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                if "-->" not in caption_pending.read_text(encoding="utf-8"):
                    raise SourceError(
                        "The English subtitle file has no usable captions."
                    )
                caption_pending.replace(caption_output)
            temporary.replace(output)
            return output
        finally:
            cleanup_temp_files(prefix)
            temporary.unlink(missing_ok=True)
            caption_input.unlink(missing_ok=True)
            caption_pending.unlink(missing_ok=True)
            workspace.cleanup()


def fetch_subtitle(url, headers):
    from urllib.parse import urljoin, urlsplit

    with niquests.Session() as session:
        session.trust_env = False
        for _ in range(5):
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.username or parts.password:
                raise SourceError("The subtitle address is not a public HTTPS URL.")
            _public_host(url)
            response = session.get(
                url, headers=headers, timeout=20, allow_redirects=False, stream=True
            )
            try:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 5 * 1024 * 1024:
                        raise SourceError("The subtitle file is too large.")
                    chunks.append(chunk)
                data = b"".join(chunks)
                text = data.decode("utf-8-sig")
                if "-->" not in text or "<html" in text.lower():
                    raise SourceError("The player returned an invalid subtitle file.")
                return data
            finally:
                response.close()
    raise SourceError("Too many subtitle redirects.")


class Movies123Episode(_EpisodeDownload):
    """Registered episode model with the usual URL-based constructor."""

    def __init__(
        self,
        url,
        season=None,
        episode=None,
        selected_path=None,
        queue_id=None,
        selected_provider=None,
        selected_language="Source Audio",
        selected_quality="Best available",
        metadata=None,
        series=None,
    ):
        if url.startswith("https://"):
            path, season_number, episode_number = selection(url)
        else:
            # Internal prototype samples used relative paths and explicit numbers.
            path, season_number, episode_number = url, season or 1, episode or 1
        super().__init__(
            path,
            season_number,
            episode_number,
            selected_path,
            queue_id,
            selected_provider,
            selected_language,
            selected_quality,
        )
        from .season import Movies123Season

        self.url = title_url(path, season_number, episode_number)
        self.season = (
            season
            if isinstance(season, Movies123Season)
            else Movies123Season(self.url, series=series)
        )
        self.episode_number = episode_number
        self.title_en = (metadata or {}).get("title", f"Episode {episode_number}")
        self.title_de = ""
        self.availability_hint = (metadata or {}).get("availability_hint", "")
        self.language_labels = ["Source Audio"]
        self.provider_data = {}
