# framevalet

Self-hosted, multi-user photo manager for Samsung Frame TVs. The family uploads
photos from any phone or laptop; framevalet optimizes them (HEIC included),
pushes them to Art Mode with a matte and the right date, and keeps everything
in sync — even when the TV is off.

## Why

The Frame is a lovely photo frame with a clumsy ingestion story: SmartThings is
one-phone-at-a-time, USB means walking to the TV, and Samsung's cloud wants an
account. framevalet gives the whole household one shared web page instead.

- **Upload anything**: HEIC/HEIF (iPhone default), JPEG, PNG, TIFF, WebP, BMP, GIF.
  Every photo is auto-oriented, converted to sRGB (no washed-out colors), resized
  to 4K, and optimized to a lean baseline JPEG before it touches the TV.
- **Queue-first**: uploads always succeed instantly. TV off? Photos wait in the
  queue and push automatically when it's back. Crashes resume; nothing is lost.
- **Multi-user with an admin panel**: local accounts, roles, per-user permissions.
  Members delete only their own uploads by default.
- **Import from the TV**: adopt everything already in My Photos (with thumbnails)
  so the app manages your existing collection, not just new uploads.
- **Respects other sources**: photos added via SmartThings show as "external" and
  are never touched automatically.
- **TV Doctor**: step-by-step diagnostics that turn every Samsung failure mode
  into the exact remote-control menu fix, plus a guided pairing wizard.
- **White-label**: brand name, logo, and accent color are configurable, so you
  can deploy it under your own identity.
- **Portable pairing**: export the TV connection (host + client name + token) as
  env vars and move the whole thing to another machine without re-pairing.

Everything is configurable in the admin panel; every setting can instead be
pinned by an env var (env always wins and the UI says so).

## Quick start (Docker)

```yaml
services:
  framevalet:
    image: ghcr.io/kdsystemsinc/framevalet:latest
    restart: unless-stopped
    ports: ["8470:8470"]
    volumes: ["./data:/data"]
```

`docker compose up -d`, open `http://<host>:8470`, create the admin account,
then follow the TV Doctor to pair (someone presses **Allow** on the TV remote,
once, ever).

## Quick start (Proxmox LXC)

On a Proxmox VE host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/kdsystemsinc/framevalet/main/proxmox/install.sh)"
```

Creates an unprivileged Debian container running framevalet under systemd.

## Requirements and honest caveats

- A Samsung **Frame** TV on the same LAN as framevalet. Tested against a 2025
  QN55LS03H; the local art API generally covers 2021+ models.
- **Samsung is actively restricting this API in newer firmware.** If it works on
  your firmware today, consider disabling the TV's automatic updates; after any
  update, run the TV Doctor to re-verify. If Samsung removes the API, framevalet
  degrades to a clearly-labeled "TV unreachable" queue rather than breaking, but
  pushing will require the USB fallback until the community finds a new path.
- Give the TV a DHCP reservation so its IP never changes.

## Environment variables (all optional)

| Var | Purpose |
|---|---|
| `DATA_DIR` | Data directory (default `./data`; the Docker image uses `/data`) |
| `PORT`, `HOST` | Listen address (default `8470`, `0.0.0.0`) |
| `TV_HOST` | TV IP; pins the admin-panel field |
| `TV_CLIENT_NAME` | Pairing identity (default `framevalet`); must travel with the token |
| `TV_TOKEN` | Paired token; lets a new machine skip the Allow popup |
| `TV_MAC` | Enables Wake-on-LAN |
| `ADMIN_USER`, `ADMIN_PASSWORD` | Headless first-boot admin creation |
| `APP_SECRET` | Session secret (auto-generated if unset) |
| `BRAND_NAME`, `BRAND_ACCENT`, `BRAND_LOGO` | White-label pinning |
| `DEFAULT_MATTE` | Matte for new photos (default `flexible_antique`, never crops) |
| `JPEG_QUALITY`, `KEEP_ORIGINALS`, `RECONCILE_MINUTES` | Pipeline/sync tuning |

## Security notes

framevalet is designed for a home LAN. Auth is required for every page and
every image byte, passwords are argon2-hashed, and sessions are server-side.
For remote access, put it behind Tailscale or a VPN; don't port-forward it.

## License

MIT © KD Systems, Inc.
