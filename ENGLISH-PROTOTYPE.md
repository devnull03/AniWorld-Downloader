# English source prototype

Branch: `english-sources`. Development settings, queue, browser profile, and
browser state lives in the ignored `.prototype-config` folder. Media and
download staging now use the SSD Jellyfin Movies and TV folders. This clone does not replace the running AniWorld installation.

**123Movies** appears in the normal source selector on the home page. It uses
the existing browse cards, search, title modal, season accordion, language and
provider selectors, download folder selector, and queue. The old `/english`
address redirects to `/?site=movies123`; the separate prototype UI is removed.
The source can be disabled in Settings → Sites, like the other sources.
TV files use `Title/Season N/Title S01E01.mkv`; movies use `Title/Title.mkv`.

The latest code discovers each title's advertised players in the background,
including Vidrock's internal servers. The existing language/provider dropdowns
fill progressively with playable HLS streams and declared audio tracks. English
subtitle combinations are offered when the player supplies captions; original
audio with English subtitles is preferred when its language is declared.
Unlabelled audio remains “Source Audio”. A Quality selector lists the selected
provider's playlist resolutions, or the resolution inspected from its video
when there is only one stream. Best available chooses the highest rendition.
Selected provider, audio, captions and quality are retained in the queue and
resolved afresh for each episode. An unavailable selection fails explicitly.

English captions are saved as a matching `.eng.srt` sidecar for Jellyfin. Stream
segments and remux files stay inside `.download-staging` on the SSD. Default
Movies/TV destinations are routed by media type; an explicitly chosen custom
folder takes priority. The UI selects the configured Jellyfin Movies or TV
folder when opening a title. Library `.ignore` files exclude temporary media.

Discovery is limited to downloadable HLS returned by the player. Players that
require unsupported layouts, iframe chains, verification or other stream
formats may be unavailable. Discovery is bounded to two pages per scan and
cached briefly. It does not bypass DRM or ignore TLS errors.

Default scans check four preset integrations: Vidnest, Vidrock (including its
internal servers), MoviesAPI and Vidrift, when advertised for the title. The
dialog's “Scan all advertised players” button explicitly enables the larger
scan, with a separate cache. Scans yield after batches of two players. New
title scans take priority over continuations. Concurrency stays at
two workers with at most two pages each. The initial dialog stays on its
loading skeleton until the complete provider scan finishes, with checked-player
progress displayed there. Subsequent episode probes use a checking placeholder;
transient API errors preserve known choices and retry. A failed default player still permits probing its advertised
internal alternatives. Selected internal-provider names are accepted by the
queue validator.

A live Tokyo Revengers preset check completed in 30 seconds with five player
checks including an internal server, and exposed English, French and Japanese
audio choices with English subtitle combinations. This is one measured run;
provider response times vary. Browser fixtures also verified that only an
explicit full-scan click requests `scan=all`.

Seasons load episode names and counts on expansion, immediately showing a
loading message and reusing the results when reopened. Empty source responses
show a zero count and an explicit missing-metadata message. Browser checks with
isolated discovery responses verified that partial scans keep the dialog
hidden and later seasons are not fetched ahead of expansion. A separate check
against real HIMYM episode responses verified season two's 22 names, beginning
with “Where Were We?”. No browser script errors occurred.

The source labels Tokyo Revengers season-one episodes “Trailer only” and
currently returns no episode links for its other seasons. That catalog warning
is displayed separately from clean episode names. It does not independently
verify the player’s media. After the fixes, the live UI found Nova with English
audio and English subtitle options for episode one, without script errors.
A three-segment Nova sample also passed audio/video validation at 1920×1080
with an English subtitle sidecar in `Media/.prototype-checks/tokyo-fix` on the
SSD. This verifies a short sample, not a complete episode.

The prototype has been reloaded with the user's approval after the HIMYM queue
finished. Backend discovery, quality selection, SSD staging and retry handling
are now active, along with corrected catalog thumbnails, SSD destinations,
Jellyfin temporary-file ignore rules and completed HIMYM episode organization.

## Changing domains

In **Settings → Sites**, edit **English source address (123Movies / GoMovies)**,
click **Check address**, then **Save address**. Checking only reads; saving
persists `ANIWORLD_123MOVIES_BASE_URL` into this instance's `.env` and applies
immediately. Enter an HTTPS origin such as `https://123movie.sx` or
`https://gomovies.gd`, not a title URL. The adapter supports their observed page
layout, not every site using those names.

Title paths are stored relative to the saved origin so pending downloads use
the updated address. Public DNS is checked before source requests; cross-domain
redirects require a manual address update. The check verifies catalog shape,
not the operator's identity or availability of every download server.
Automatic discovery of replacement domains is not implemented.

## Local preview

Dependencies: the project's Python packages, FFmpeg, FFprobe, and Chromium.
If Chromium is missing, run `.venv/bin/python -m patchright install chromium`.
An existing Playwright Chromium installation is also detected. A custom browser
path can be set with `ANIWORLD_123MOVIES_BROWSER_EXECUTABLE`.

Use a fresh development config rather than the production installation:

```sh
mkdir -p .prototype-config
touch .prototype-config/.env
ANIWORLD_INSTALL_FOLDER="$PWD/.prototype-config" \
ANIWORLD_DOWNLOAD_PATH="/Volumes/Extreme SSD/Media/TV" \
ANIWORLD_NO_AUTO_INSTALL=1 \
.venv/bin/aniworld --web-ui --web-port 8083 --no-browser
```

Open http://127.0.0.1:8083/?site=movies123 .
Private Tailscale preview: https://dodo.anoa-themis.ts.net/?site=movies123 . The preview binds to loopback by default.
Creating the empty `.env` first prevents relocation from copying settings from
an existing install.

## Verification

After the authorized reload, the real Tailscale browser showed 60 catalog
cards with the first eight thumbnails loaded, a quality selector and the
Jellyfin TV destination. HIMYM discovery returned Orion, Vidnest, MoviesAPI
and Vidrift with progressively populated audio, subtitle and quality choices.
A previously failed HIMYM S01E02 completed a three-segment Vidnest sample at
1920×1080 with usable audio and an English subtitle sidecar, stored outside
the libraries in `Media/.prototype-checks/reload`. The legacy Vidrock and
selected Orion attempts for that episode still failed after bounded retries;
availability varies by episode and provider. This verifies a short sample,
not a complete episode download.

Live checks on 2026-10-04:

- Both `123movie.sx` and `gomovies.gd` returned Reacher search results, four
  seasons, and eight available episodes in the tested seasons.
- The browser UI selected an episode and submitted it to the existing queue.
- Reacher S01E01 completed as a full 54-minute MKV containing H.264 video at
  720×360 and AAC audio (116,507,161 bytes), with no queue errors.
- Automatic live checks found Vidnest resolutions of 960p/640p and Vidrock
  Nova/Orion streams. A Vidnest 640p sample was verified as 1280×640 with usable
  audio/video and a matching English subtitle sidecar. An Orion sample also
  passed media and caption checks. Samples are on the SSD below
  `/Volumes/Extreme SSD/Media/.prototype-checks`, outside the Jellyfin libraries.
- HIMYM completed episodes were moved into `TV/How I Met Your Mother/Season 1`;
  Jellyfin indexed one series with seasons and episodes. The original queue
  remained running throughout; an organizer catches any final episode written
  to the former Movies destination.
- Separate short samples from S01E02 on 123Movies and S02E02 on GoMovies passed
  audio/video validation. Samples are explicitly named `.sample.mkv`.

Offline tests cover parsing, domain settings and persistence, API access,
queue integration and failures, and normalization of the observed PNG-prefixed
transport-stream segments, including serial and parallel download settings.

Download resilience changes are active in the reloaded prototype:

- Legacy queue entries now accept master playlists with multiple qualities.
- Player timeouts and missing playlists get at most three fresh attempts with
  exponential backoff and jitter; invalid selections are not retried.
- Playlists and segments retry transient HTTP failures. HTTP 429/503 share a
  host cooldown across workers and honor `Retry-After` seconds or HTTP dates.
  Cooldowns over 60 seconds stop the attempt without retrying early.
- Permanent HTTP errors, including 403/404/410, stop immediately. Queue errors
  identify the host and status without including signed media URLs.
- English-source segment downloads use at most three workers, preserving a
  lower user-configured concurrency. Other sources retain their configured cap.

The observed HIMYM queue errors said no usable playlist was captured; they do
not establish that the host was rate limiting. A later isolated probe also
failed to open the advertised player. The previous queue finished all 44
entries with 13 errors before the user authorized the reload.

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check
.venv/bin/ruff format --check
```

This remains a prototype: source layouts and third-party server availability
can change. Movie selection is implemented, but live playback verification covers TV episodes. The user-downloaded Your Name movie was verified as a
106-minute 1080p file and moved into Jellyfin’s Movies library as
`Your Name (2016)/Your Name (2016).mkv`; Jellyfin indexed it successfully.
