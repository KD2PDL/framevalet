# Second-opinion audit review (Codex / GPT-6) — reconciled with Claude's audit

A second independent audit was run by Codex (GPT-6). It hit its usage limit
before writing its own report or applying its patch, but left a tested patch in
`/tmp/framevalet-audit`. This document reconstructs its findings from that patch,
records Claude's verdict on each, and notes what was applied. Findings are on top
of Claude's first-pass hardening (SSRF, watcher guard, packaging, import-from-TV,
Art Mode guards, upload caps, login rate-limit, security headers).

## Confirmed real — APPLIED

| # | Severity | Finding | Where | Fix |
|---|----------|---------|-------|-----|
| 1 | **High** | **Login rate-limit bypass.** The throttle keyed on the client-supplied `Cf-Connecting-Ip` header; when the app is *not* actually behind Cloudflare (the common case), an attacker rotates that header to get unlimited login attempts. Claude introduced this in first-pass. | `routes.py` login_post | Key the limiter on `request.client.host` only. |
| 2 | **High** | **Blocking I/O on the async event loop.** `async def` handlers ran Pillow decode (`ingest_bytes`) and synchronous TV socket calls (`port_open`, `change_matte`, `select`) directly on the loop, so one upload or one unreachable-TV call stalls *every* concurrent request for the socket timeout / decode time. | `routes.py` upload, set_matte, save_crop, set_tags, display, bulk | Offload CPU work with `asyncio.to_thread`; convert TV-touching JSON handlers to sync `def` so Starlette runs them in the threadpool. |
| 3 | **High** | **`kick()` is thread-unsafe.** `asyncio.Event.set()` was called from sync route handlers running in worker threads; `asyncio.Event` is not thread-safe and must be signalled on the loop thread. Could wedge the worker wakeup. | `worker.py` kick | Signal via `ws._loop.call_soon_threadsafe`. |
| 4 | **Medium** | **TV art-channel connection leak.** `self._tv.art()` returns a fresh art client (own websocket) on every `_art()` call; `reset()` only closed the parent remote, leaking the art connection each retry. Over time exhausts sockets on the TV/host. | `tvservice.py` | Cache the art client, close it explicitly in `reset()`. |
| 5 | **Medium** | **WebSocket: no Origin check, no re-validation.** The `/ws` handshake didn't check `Origin` (cross-site WebSocket hijack; mitigated by SameSite but defense-in-depth), and a session that expired/was disabled stayed connected forever. | `ws.py` | Reject cross-origin handshakes (4403); re-validate the session every ~25s and drop on expiry. |
| 6 | **Medium** | **No same-origin enforcement on state-changing requests (CSRF).** Relied solely on SameSite=Lax. | new `security.py` `RequestGuard` | Middleware requires a same-origin `Origin`/`Referer` on non-GET requests, plus a hard request-size ceiling. |
| 7 | **Low** | **Non-integer settings crash later.** `jpeg_quality`, `reconcile_minutes`, `watch_interval`, `rclone_interval` were stored unvalidated, then `int()`-parsed in the worker → ValueError. | `config.py` set | Validate integer range on write. |
| 8 | **Low** | **Session cookie `Secure` not settable.** Correct default for LAN HTTP, but no way to turn it on behind TLS. | `config.py`, `auth.py` | `COOKIE_SECURE` env (default off). |
| 9 | **Low** | **CSP / clickjacking.** X-Frame-Options was set but no CSP `frame-ancestors`; authed pages were cacheable. | `main.py` | Add `Content-Security-Policy: frame-ancestors 'none'; object-src 'none'; base-uri 'self'`; `Cache-Control: no-store` on authed pages. |
| 10 | **Low** | **Retry sleeps after the final attempt.** `_call` slept `5*(i+1)s` even on the last failed attempt, adding latency to every hard failure. | `tvservice.py` | Skip the sleep on the last iteration. |
| 11 | **Low** | **Matte value not type-checked.** A non-string matte in crafted JSON reached `.split()`. | `routes.py` | `_validate_matte` type + value check, shared by single and bulk. |

## Considered — NOT adopted (with reason)

| Finding | Codex's change | Why not adopted |
|---------|----------------|-----------------|
| Folder-watcher mirror is a data-loss risk | Made the watcher **fully non-destructive** (never deletes from folder; only reports missing count) | You explicitly chose **full-mirror** semantics ("removed from folder = removed from library and TV"). Claude's first pass already added guards that block the *catastrophic* case (empty/failed/>50% delete) while still honoring a legitimate single deletion. Keeping guarded-mirror honors your requirement; Codex's stance is the more conservative option if you later prefer it. Documented in README as a tradeoff. |
| Import-from-TV push path concurrency | Complex `BEGIN IMMEDIATE` re-check of latest edits mid-push | Real but low-probability (a crop saved in the exact window of an in-flight push to that TV). Claude's render-key staleness already forces a corrective re-push on the next loop, so the outcome self-heals. Deferred to avoid importing a large, hard-to-review transaction rework. |

## Verified clean by both audits
Parameterized SQL throughout, Jinja autoescape with no `|safe` and `textContent`-only JS sinks (no XSS), path traversal defended (`Path(name).name`), sound session tokens/logout, no client data in the repo (only the project logo/icon and placeholder example IPs), MIT license present, no copied third-party code.
