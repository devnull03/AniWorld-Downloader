# Branch-testing container

The `english-sources` branch is deployed as `aniworld-downloader-testing` at
https://aniworld.anoa-themis.ts.net. Tailscale's existing `svc:aniworld` proxy
still targets host loopback port 8082. The upstream `aniworld-downloader`
container is stopped and retained for rollback.

`Dockerfile.testing` replaces application code in the pinned, verified upstream
runtime. It retains that runtime's Chromium, FFmpeg, and Python dependencies;
use the full `Dockerfile` when dependencies change. The image revision label
records the application checkout used to build it.

The test instance uses a consistent snapshot of the original users, settings,
queue, and session secret in `.docker-testing-config/`, excluded from Git and
Docker build context. The original `~/.aniworld` state is retained for rollback.
New testing settings and queue entries remain in the testing config.

Anime, TV, and Movies are bound from the SSD to `/media/Anime`, `/media/TV`, and
`/media/Movies`. The original `/app/Downloads` Anime alias is also retained.
Movies123 has Movies and TV destinations; uniquely matching existing Anime
series are reused automatically. Host-side paths must not be entered into the
container's download settings.

The active Compose file is installed at
`/Users/devnull/Projects/mac-media-server/aniworld/compose.yml`, which the
existing `local.aniworld.server` SSD supervisor manages. Docker restart remains
disabled, and missing bind-mount sources are never created automatically.

## Rebuild

From the repository root:

```sh
docker build -f Dockerfile.testing --build-arg VCS_REF="$(git rev-parse HEAD)" \
  -t aniworld-downloader:english-sources-testing .
```

Check that the queue is idle before restarting through the supervisor. Do not
start a second container against the same testing configuration.

## Rollback

The original Compose file is saved locally in
`.deployment-backups/upstream-compose.yml` (excluded from Git/build context).
Once the testing queue is idle, from the repository root:

```sh
launchctl bootout gui/$(id -u)/local.aniworld.server
docker stop --timeout 15 aniworld-downloader-testing
cp .deployment-backups/upstream-compose.yml \
  /Users/devnull/Projects/mac-media-server/aniworld/compose.yml
launchctl bootstrap gui/$(id -u) \
  /Users/devnull/Library/LaunchAgents/local.aniworld.server.plist
```

The supervisor checks the SSD before bringing the upstream container back.
Shared media files remain on the SSD; the testing configuration is preserved.

## Verified deployment

- Container health check, SSD writes to all three libraries, and Chromium launch.
- Existing user/password records and session secret match the original instance.
- Existing-user in-process UI smoke checks: Movies123 page, queue, and settings.
- A short Vidnest S02E05 sample downloads/remuxes with Japanese audio inside
  Docker, passes FFprobe validation, and is cleaned up from hidden SSD staging.
- Existing Anime series and `Season 02` resolve correctly from the TV destination.
- HTTPS login returns 200 through the approved AniWorld Tailscale service IP
  with hostname/certificate validation. This Mac's default resolver did not
  resolve the service name, so the check used curl's `--resolve` option.

## Catalog discovery

123Movies uses the shared home-page discovery row. Genre, type, country, release
year, quality, and sort options come from the configured site's `/browser`
filter form. Expand a group and select tags to combine filters; sort allows one
selection. Load more retains those selections, and Clear filters restores the
normal browse view. The quality tags describe the catalog's HD/CAM labels; the
download dialog still discovers the playable stream's available resolutions.

Single genre/country selections use native category pages. Combined filters
use the native browser query and, when required, the existing visitor session.
Failed upstream requests are reported rather than rendered as empty results.
Changing the saved site URL uses separate discovery and metadata caches.
