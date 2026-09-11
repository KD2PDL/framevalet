# SAWSUBE (WB2024) Research Report

Self-hosted Samsung Frame TV art manager: FastAPI + SQLAlchemy (aiosqlite) backend, React frontend,
APScheduler rotation, watchdog folder ingest, single WebSocket push channel. Repo has NO license;
everything below is described as ideas/techniques to reimplement independently, with no code copied.

Sources examined: full backend tree at commit `db3d943` (main), issues #1 and #2 with all comments,
and the NickWaterton samsung-tv-ws-api fork (master, pushed 2026-04-06).

---

## 1. samsungtvws usage (NickWaterton fork)

### Pinning and the PyPI trap
- `requirements.txt` pins `samsungtvws[async,encrypted] @ git+https://github.com/NickWaterton/samsung-tv-ws-api.git`.
- A comment in requirements plus issue #1 explains why: the PyPI `samsungtvws` (xchwarze) and the
  NickWaterton fork BOTH currently report version **3.0.5** but are **different codebases**. Their
  Docker build originally pulled PyPI and the import of `samsungtvws.async_art` failed silently at
  startup (guarded try/except), so the app booted fine but every TV endpoint returned
  500 "samsungtvws library not installed". If we ever use the fork, pin a commit SHA, and make the
  import failure loud, not a boot-time warning.

### What the fork offers over PyPI 3.0.5 (relevant if we stay sync)
- Explicitly targets "extended support to Frame TVs including 2021/22/23 and **2024** models";
  README recommends the async library as "more reliable/robust" for art mode.
- `async_art.SamsungTVAsyncArt`: persistent websocket to the `com.samsung.art-app` channel with an
  event loop (`start_listening`), a pending-request table keyed by request UUID, and per-request
  `asyncio.Future` + `wait_for_response(timeout=2s default)`.
- Handles BOTH old and new art API dialects: sends both `id` and `request_id` fields (old API uses
  `id`, new uses `request_id`); correlates replies by either. `get_api_version` falls back from
  request name `get_api_version` to legacy `api_version`.
- Tracks art-mode state passively from D2D service-message events: `artmode_status` /
  `art_mode_changed` / `go_to_standby` / `wakeup` sub-events, with an API 5.x quirk where the field
  is `status` instead of `value`. User-registerable callbacks per sub-event.
- Recent maintenance (active): Dec 2025 added `set_brightness_sensor_setting`, `set_motion_timer`,
  `set_motion_sensitivity`, reworked upload (URL streaming support); **Apr 2026 merged a fix for a
  KeyError in event processing on "API 5.x TVs"** (i.e., newest firmware). 192 stars, single
  maintainer + PRs. So: alive, but low-bus-factor.

### SAWSUBE connection lifecycle
- One `TVConnection` object per TV row, cached in a `TVManager` dict; holds separate lazily-created
  `SamsungTVAsyncArt` and `SamsungTVWSAsyncRemote` handles (same token file for both), an
  `asyncio.Lock`, and `last_status`.
- Handles are created on first use and `start_listening()` is awaited immediately; on any exception
  in any operation the art handle is set to `None` so the next call reconnects (drop-and-lazy-reconnect,
  no active retry loop inside operations).
- A per-TV background poll task runs every `POLL_INTERVAL_SECS` (default **20s**): fetch
  `get_artmode` + `get_current`, update `last_seen` in DB, and broadcast over the app WebSocket only
  when `(online, artmode, current)` actually changed (transient `error` string is ignored for the
  change check). On poll exceptions: exponential backoff 5s → doubles → capped at 60s.
- Pairing: reset connection, then connect with a **15s** `asyncio.wait_for` — connecting to the art
  channel is what triggers the TV's Allow/Deny prompt. After connect, check the token file exists
  and is non-empty. If connected but no token was issued (TV in developer mode accepts silently),
  they write a placeholder token file so the UI shows "paired". `paired` in status = token file exists.
- Shutdown: cancel poll tasks, close all handles (lifespan handler).

### Frame generation / firmware handling
- None in SAWSUBE itself beyond delegating to the fork — and that is its weak spot; see §8.

## 2. Image pipeline (Pillow)

Accepted formats: **.jpg .jpeg .png .webp .bmp** (extension gate) plus a Pillow `open().verify()`
sniff on upload; invalid files are deleted and rejected 400.

Pipeline order (their `process_image`):
1. **Hash-first caching**: SHA-256 of the source file (streamed in 1 MiB chunks). Output path is
   `cache/{sha256}.jpg`; if it exists, return it immediately. Hash is also the DB dedup key
   (unique index) — re-uploading the same bytes returns the existing row.
2. **EXIF orientation**: `ImageOps.exif_transpose`. Gotcha they handle: transpose builds a NEW image
   and drops `info["icc_profile"]`, so they capture the ICC bytes before transposing and restore
   them after if missing.
3. **ICC → sRGB**: if an embedded ICC profile exists, `ImageCms.profileToProfile` from the embedded
   profile to `ImageCms.createProfile("sRGB")`, output mode RGB; on any failure, fall back to plain
   `convert("RGB")`. No profile → just RGB convert.
4. **Orientation decision**: aspect ratio `< 1.3` is treated as "portrait-ish" (catches squares and
   4:3, not just true portrait). Three configurable modes (`PORTRAIT_HANDLING`, default `blur`):
   - **blur** (blur-fill): background = copy scaled to *cover* the target (scale = max of the two
     axis ratios), center-cropped to target, `GaussianBlur(radius=40)`, then desaturated with
     `ImageEnhance.Color` factor **0.7**; foreground = fit-by-height (fallback fit-by-width if too
     wide), LANCZOS, pasted centered onto the blurred background.
   - **crop**: center-crop to target aspect then resize.
   - **skip**: letterbox on solid black, fit by height, centered.
   Landscape images always get center-crop-resize.
5. **Sharpen**: `UnsharpMask(radius=1.0, percent=30, threshold=3)` applied after resize (subtle).
6. **EXIF strip + encode**: paste the processed image into a brand-new RGB image (drops all
   metadata), save JPEG `quality=95, optimize=True`.

Targets: `TV_RESOLUTION` env, `4K` → **3840×2160**, else 1920×1080. All resizes use `Image.LANCZOS`.
Thumbnails: width **400px**, proportional height, JPEG `quality=85, optimize=True`, cached as
`thumbnails/{hash}_{width}.jpg`. All Pillow work runs via `asyncio.to_thread`.

Folder-watch ingest (watchdog) adds two practical guards: a 2s debounce with cancel-and-replace
tasks per path (created/modified events), then a "file size stable" loop (up to 5 × 0.5s waits,
size must repeat and be > 0) to avoid ingesting a file mid-copy (noted as a Windows lock workaround).

## 3. TV discovery + Wake-on-LAN

### SSDP scan
- Single UDP socket, `SO_REUSEADDR`, timeout ~3s, sends M-SEARCH to 239.255.255.250:1900 twice:
  once with `ST: urn:samsung.com:device:RemoteControlReceiver:1` and once with `ST: ssdp:all`
  (broader catch). `MX: 2`. Collects responder IPs whose raw response contains "samsung"
  (case-insensitive) until socket timeout.
- Each candidate IP is then probed over HTTP: `http://{ip}:8001/api/v2/` then
  `https://{ip}:8002/api/v2/` (TLS verify off, 3s timeout). A 200 JSON response yields
  `device.modelName`, `device.name`, `device.type`, `device.wifiMac`, and crucially
  **`device.FrameTVSupport == "true"`** as the Frame detector. This REST probe is the reliable part;
  SSDP is only a candidate generator. (The same endpoint also exposes `PowerState`, `TokenAuthSupport`,
  `developerMode`, `resolution` — see the device dump in issue #1.)
- Docker caveat (from docker-compose comment + issue #1): SSDP multicast does not cross the Docker
  bridge network. `network_mode: host` is recommended (commented out in their compose file) on
  Linux for SSDP + WoL; on Docker Desktop (Mac/Windows) host networking doesn't behave the same, so
  the documented workaround is manual IP entry.

### Wake-on-LAN
- Just the `wakeonlan` package's magic packet to the stored MAC; that IS their "power on" (there is
  no WS power-on path). Power off = remote channel `KEY_POWER`. The `wifiMac` harvested during
  discovery feeds this. WoL also needs host networking (or broadcast routing) in Docker.

## 4. Art Mode control surface (request names worth knowing)

SAWSUBE exposes: brightness, color temperature, motion timer, motion sensitivity, brightness
sensor, slideshow shuffle/interval, matte per image, art mode on/off, current artwork get/set,
TV-side thumbnails, matte list, device info. All via fork methods; underlying art-channel
`request` names (from the fork — useful to us for raw implementation):

| Purpose | Request name | Values / notes |
|---|---|---|
| API version | `get_api_version` (new), `api_version` (old) | returns `version` |
| Device info (art channel) | `get_device_info` | |
| List content | `get_content_list` | optional `category`; categories `MY-C0002` = My Pictures, `MY-C0004` = Favourites, `MY-C0008` = Store; response `content_list` is a JSON string; filter client-side by `category_id`. Longer timeout (4s) |
| Current art | `get_current_artwork` | returns `content_id` |
| Favourite | `change_favorite` | `status` on/off; wait for event `favorite_changed` |
| All art-mode settings | `get_artmode_settings` | returns JSON-string list of `{item, value}`; items: `brightness`, `color_temperature`, `motion_sensitivity`, `motion_timer`, `brightness_sensor_setting`. **Fallback when per-setting getters return nothing (newer firmware)** |
| Brightness | `get_brightness` / `set_brightness` | "0".."10" (newer TVs may only answer via `get_artmode_settings`) |
| Color temp | `get_color_temperature` / `set_color_temperature` | "-5".."5" |
| Brightness sensor | `set_brightness_sensor_setting` | "on"/"off" |
| Motion timer | `set_motion_timer` | "off","5","15","30","60","120","240" (minutes) |
| Motion sensitivity | `set_motion_sensitivity` | "1".."3" |
| Slideshow (newer TVs) | `get_slideshow_status` / `set_slideshow_status` | `value` = minutes as string or "off"; `type` = "slideshow" or "shuffleslideshow"; `category_id` = "MY-C0002"/"MY-C0004" |
| Slideshow (older TVs) | `get_auto_rotation_status` / `set_auto_rotation_status` | same shape — old firmware calls the feature "auto rotation" |
| Art mode | `get_artmode_status` / `set_artmode_status` | `value` "on"/"off" |
| Rotation (physical) | `get_current_rotation` | `current_rotation_status` |
| Photo filters | `get_photo_filter_list` / `set_photo_filter` | `filter_list` JSON string; e.g. filter id "ink" |
| Thumbnails | `get_thumbnail_list` (new) / `get_thumbnail` (old) | TV opens a side socket; read 4-byte BE header length, JSON header, then `fileLength` bytes |
| Upload | `send_image` | see below |
| Delete | `delete_image_list` | `content_id_list` = list of `{content_id}` objects |
| Select | `select_image` | `content_id`, optional `category_id`, `show` bool (false = queue without displaying) |
| Matte list | `get_matte_list` | returns `matte_type_list` + `matte_color_list` (JSON strings); matte id format is `type_color`, e.g. `modern_apricot`, `shadowbox_polar`, or `none` |
| Change matte | `change_matte` | `content_id`, `matte_id`, optional `portrait_matte_id` — **portrait matte is a separate field**; "Not all mattes can be set for all image sizes" (fork comment) |

Upload flow (`send_image`) mechanics: request carries `file_type` (note: "jpeg" must be sent as
"jpg"), `conn_info` `{d2d_mode: "socket", connection_id: random < 2^32, id: uuid}`, `image_date`
("%Y:%m:%d %H:%M:%S"), `matte_id`, `portrait_matte_id`, `file_size`. The TV replies with its own
`conn_info` (ip, port, `key`, `secured` flag). Client then opens a raw TCP socket to that ip:port —
**TLS-wrapped if `secured`** (newer firmware) — writes a 4-byte big-endian header length, an ASCII
JSON header `{num:0, total:1, fileLength, fileName:"dummy", fileType, secKey:<conn_info.key>,
version:"0.0.1"}`, then the image bytes in **64 KiB chunks**, closes, and waits (10s) for the
`image_added` event carrying the new `content_id`. Same UUID must appear as both `id` and
`request_id` in the request. SAWSUBE uploads its processed JPEG with `matte="none"`.

Small SAWSUBE bug worth avoiding: their slideshow call passes `type="shuffle"`/"serial" strings
into a parameter the fork treats as a boolean, so it always resolves to shuffleslideshow.

## 5. Scheduling design (conceptual)

- Data model per schedule row: tv_id (FK, cascade), name, **mode: random | sequential | weighted**,
  `source_filter` JSON (keys used: `favourites_only`, `source`, `tag` substring), `interval_mins`
  (default 60, min 1), optional `time_from`/`time_to` (TIME columns), `days_of_week` as CSV string
  "0,1,2,3,4,5,6" (Python weekday numbers), `is_active`, `last_index` (cursor for sequential mode).
- Engine: APScheduler `AsyncIOScheduler` with one `IntervalTrigger` job per schedule (job id
  `sched_{id}`, `replace_existing`), installed/removed on CRUD and all reloaded at startup. The job
  always fires on interval; the **time window and day filter are checked inside the job** and it
  no-ops outside the window. Window comparison supports spanning midnight (from > to → OR logic).
- Selection: query eligible images per filter; exclude the last **5** shown on that TV (per-TV
  History table with `trigger` = schedule/manual) unless exclusion would empty the pool; weighted
  mode gives favourites weight **3 vs 1**.
- Fire path: ensure image is processed (lazy), ensure a TVImage link with a `remote_id` exists
  (upload on demand), then `select_image(show=True)`, record History, broadcast `schedule_fired`.

## 6. Realtime updates (WebSocket)

- One global endpoint `/ws`; server never parses client messages (reads and discards as keep-alive).
- A singleton broadcast manager: set of sockets under an asyncio lock; broadcast JSON to all with
  `asyncio.gather(return_exceptions=True)` and prune sockets whose send raised.
- Message types (all `{type, ...}` JSON): `tv_status` (from the 20s poll loop, only on change),
  `art_changed` (after select), `image_added` (upload/watcher ingest), `schedule_fired`,
  `sync_progress` `{tv_id, done, total, filename, status: ok|skipped|error}` and `sync_complete`
  `{uploaded, failed, skipped}` for the bulk "sync library → TV" background task. Per-file upload
  progress to the TV itself is NOT streamed — granularity is per image.
- Bulk sync is a fire-and-forget `asyncio.create_task`; the HTTP response returns
  `{queued, already_on_tv}` immediately and progress arrives over WS.

## 7. Multi-TV data model

- `tvs` table: name, ip, mac, port (default **8002**), `token_path`, model, year, last_seen.
- **One token file per TV**: `tokens/tv_{id}.token`, path stored on the row; shared by the art and
  remote websocket clients for that TV. Paired = token file exists and is non-empty (with the
  developer-mode placeholder exception).
- `tv_images` join table maps library image ↔ TV: `remote_id` (the TV's content id, e.g. MY-Fxxxx),
  `is_on_tv` soft flag, per-TV `matte` string. Same library image can live on several TVs with
  different remote ids/mattes. History is also per-TV.
- Per-TV art settings (brightness etc.) are NOT persisted locally — read/written live from the TV.
- Connection cache is keyed by tv_id; row edits refresh ip/mac/name on the cached connection.

## 8. Issues, pitfalls, gotchas

- **Issue #1 (open, the big one): pairing fails on 2024 Frame LS03D.** Reporter device:
  QN50LS03DAFXZA, model string `24_PONTUSM_FTV`, `FrameTVSupport: true`. Log signature: websocket
  URL `wss://ip:8002/api/v2/channels/com.samsung.art-app?...&token=None`, immediately answered by
  `ms.channel.clientDisconnect` with `token: "None"`, in a loop. Also reproduced on a 2023 LS03C
  (QA32LS03CBKXXT) and on a **non-Frame** QN55Q60CA, in Docker and native Linux alike. Unresolved in
  the thread as of 2026-04-30. Takeaways for us: (a) the literal string "None" being sent as the
  token query param is a smell — first-connect should omit the token; (b) their retry behavior made
  the TV's Allow prompt **re-pop every ~2 seconds**, because each reconnect attempt spawns a fresh
  prompt — during pairing you must open ONE connection and hold it while the user hits Allow, not
  reconnect-poll; (c) the maintainer's fix direction: keep the single connection open and poll the
  token file every 2s for up to 60s (the shipped code settled on a 15s single wait, which is
  arguably too short for a human to grab the remote).
- **PyPI vs fork confusion** (issue #1): same import name, same version number, different code.
  Silent optional-import guards turn a packaging mistake into runtime 500s.
- **SSDP in Docker**: multicast doesn't cross bridge networks; host networking required on Linux;
  Docker Desktop can't do it → manual IP entry path must exist in the UI.
- **API 5.x firmware**: fork's Apr 2026 fix ("KeyError in process_event for API 5.x TVs") and its
  `status`-vs-`value` handling for `artmode_status` events confirm Samsung keeps changing event
  field names; parse defensively with fallbacks.
- Newer firmware may not answer individual `get_brightness`/`get_color_temperature` requests —
  fall back to the aggregate `get_artmode_settings`.
- Old firmware uses `auto_rotation_status` request names where new uses `slideshow_status` — support
  both if targeting mixed fleets.
- Thumbnail/upload side-channel sockets can be TLS ("secured" flag in conn_info) on newer firmware.
- Matte behavior: portrait matte is a separate id; not all mattes are valid for all image sizes
  (expect errors on `change_matte`); matte ids come from `get_matte_list` as type × color combos.
- Art channel error responses carry an `error_code`; the fork surfaces them as exceptions keyed to
  the originating request name — worth mirroring.
- REST device-info endpoint should be rate-limited client-side (fork sleeps 100ms under a lock
  before each hit).
- No explicit handling anywhere of TV storage limits, upload size limits, or art-app rate limiting;
  SAWSUBE's only mitigations are 4K-max JPEG output (keeps files a few MB) and per-image serial
  upload in bulk sync. Issue #2 is about art-source APIs (Rijksmuseum key requirement), not TVs.
- SQLite via aiosqlite, tables created at startup (no Alembic); one-time DB rename migration done
  by file copy. Fine at this scale.

## Quick verdict for our reimplementation

Worth stealing (as ideas): the processed-file cache keyed by SHA-256 (dedup + idempotent
re-processing), ICC-preserve-across-exif_transpose gotcha, ratio<1.3 blur-fill recipe
(blur 40 / desat 0.7 / unsharp 1.0-30-3 / q95), FrameTVSupport REST probe as the real discovery
filter (SSDP only as candidate list), per-TV token files + tv_images join with per-TV remote_id and
matte, change-only status broadcasting, watchdog debounce + size-stabilization, and the full
art-channel request catalog above (especially `get_artmode_settings`, `set_auto_rotation_status`,
`change_favorite`, `get_photo_filter_list`, `set_photo_filter`, `get_current_rotation`, and the
portrait_matte_id field). Treat 2024+ LS03D pairing as a known risk: hold one connection open
during pairing, never send a "None" token string, and give the user at least 60s to press Allow.
