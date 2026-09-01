# Incident 2026-09-01 — IPCA auto-assign missed (gateway down)

**Symptom:** `genericpro` posted `I Signed the IPCA` at `2026-08-31 19:28 ET` in a support ticket thread. Norozo did not auto-assign `Available` + `DEV Team` roles. Manual test by maintainer in same channel succeeded later.

**Impact:** Single user missed auto-assign. Thread was archived by staff at `~02:49 ET` before next gateway reconnect, so catch-up sweep correctly ignored it (archived tickets are intentionally not re-processed).

## Timeline (UTC)

- `2026-08-30 15:07` — Deploy live, `Shard ID None has connected` successful.
- `2026-08-31 23:28` — `genericpro` IPCA message (gateway down, no `on_message`).
- `2026-09-01 02:49` — Staff archived thread.
- `2026-09-01 04:46:36` — `Logged in as Norozo#6197` after extended disconnect. Health checks (`HEAD /health 200`) were healthy the whole window — web server stayed up, only Discord gateway was down.

## Root cause

Render shared egress IP `74.220.48.29` was Cloudflare-1015-banned by `discord.com` (ban affects all tenants on that IP). Norozo’s retry loop (`main.py: _connect_discord_with_retry` / `_is_discord_rate_limit_error` detecting `429`/`1015`/`cloudflare`/`rate limit`) backs off exponentially (`max_backoff 300s`) but remains on the same banned egress IP, so `bot.start()` keeps failing with `1015`.

The fix for this class of failure exists in code since `09faea0`/`9e1c55a`:

- `platform-services/backend/deepiri-proxy` (standalone `tinyproxy` on a VPS with a stable IP, `docs/INSTALL.md`) provides an authenticated HTTP forward proxy.
- `main.py: _discord_proxy_kwargs()` routes `discord.py` REST + gateway through `DISCORD_PROXY_URL` (`proxy=` / `proxy_auth=`).

**Production was not wired to it.** `DISCORD_PROXY_URL` was not set in the Render service environment (not in repo `.env`, not in `docker-compose.yml` env, not in Render dashboard vars), so `main.py:143` returned `{}` and all Discord traffic used the banned direct egress. The gap lasted until the `1015` ban expired / IP rotated at `04:46 UTC`, when the same binary reconnected without any code change (evidence: `RESUMED` flaps before and after `04:46`).

## Verified (no prod change)

- `deepiri-proxy` container is running on its VPS (`deepiri-proxy Up 2 days`, `0.0.0.0:8888->8888`, `.env` present, `BasicAuth` required) — ready to use. Credentials are stored only on the VPS (`~/deepiri-proxy/.env`) and never committed.
- Local/Norozo `.env` intentionally has no `DISCORD_PROXY_URL` (local dev not egress-banned); code path is `if not DISCORD_PROXY_URL: return {}`.

## Fix (no secrets in repo)

Set the Render service environment variable (not a repo file, not GitHub Actions secret):

1. Render Dashboard → `deepiri-norozo` service → `Environment` → add:

   ```
   DISCORD_PROXY_URL=http://<PROXY_USER>:<PROXY_PASS>@<VPS_IP>:8888
   ```

   - `<PROXY_USER>` / `<PROXY_PASS>` / `<VPS_IP>` are the values from the VPS `~/deepiri-proxy/.env` and its host. Do not paste real values into git, chat, or this doc — copy from the VPS directly.
   - Format is `http://` (not `socks5://`); `discord.py`/`aiohttp` only supports `http://` proxy for `proxy=`/`proxy_auth=` without extra deps.

2. `Save` → `Manual Deploy` (or wait for next `main` deploy). Watch Render logs for:

   - No `Discord 429/1015 detected — Render IP 74.220.48.29 Cloudflare ban` warning
   - `Shard ID None has connected` / `has successfully RESUMED` without preceding `Discord startup failed` exception
   - `IPCA sweep: scanning ... open thread(s)` still runs on reconnect (open-only sweep is intentional)

3. Keep `deepiri-proxy` firewall restricted if possible and rotate `PROXY_USER`/`PROXY_PASS` if ever exposed (see `deepiri-proxy/docs/INSTALL.md` security notes).

## Why archived tickets stay missed

`sweep` at `main.py:288` scans only `channel.threads` (open). Archived threads after staff action are intentionally not re-processed. Changing that would auto-assign roles to users whose tickets staff already closed. This incident is therefore not a sweep bug.

## Follow-up

- Add Render alert on `Discord startup failed` / `1015` in logs (or a health check that verifies gateway `connected`, not just `HEAD /health 200`).
- Consider a Render disk or external check that pages when `DISCORD_PROXY_URL` is unset in prod but `deepiri-proxy` is expected.

## Verification evidence for this report

- `2026-09-01` Render logs: `HEAD /health 200` every ~13 min throughout, `RESUMED` flaps at `06:41`, `07:58`, `10:24`, `11:22`, `11:43` after `04:46` reconnect — gateway-only failure.
- Code references: `main.py:44` (`DISCORD_PROXY_URL`), `main.py:133` (`_discord_proxy_kwargs`), `main.py:1281` (`_is_discord_rate_limit_error`), `main.py:1409` (`_connect_discord_with_retry`), `deepiri-proxy/docs/INSTALL.md` wiring docs.
- VPS probe: `docker ps` shows `deepiri-proxy` up, `tinyproxy` `BasicAuth` path in `entrypoint.sh`.
