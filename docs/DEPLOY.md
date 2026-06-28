# Norozo production deployment (Render)

Norozo runs on [Render](https://render.com) as a Docker web service. CD builds the image on every push to `main`, pushes it to GHCR, then hits Render’s deploy hook so production pulls the new image.

## What CD does

On push to `main`:

1. Runs tests (pytest + ruff)
2. Builds and pushes `ghcr.io/team-deepiri/deepiri-norozo:latest` and `ghcr.io/team-deepiri/deepiri-norozo:sha-<short-sha>`
3. POSTs to `RENDER_DEPLOY_HOOK_URL` with `imgURL` set to the new SHA tag
4. Optionally posts success/failure to Discord (`DISCORD_WEBHOOK_URL`)

Render does **not** auto-detect new GHCR tags — the deploy hook is required.

## One-time Render setup

In the Render dashboard for the Norozo service:

1. **Service type** — Web Service (Docker), image from registry
2. **Image URL** — `ghcr.io/team-deepiri/deepiri-norozo:latest` (must match the repo image path; CD can override the tag per deploy via `imgURL`)
3. **Registry credentials** — if the GHCR package is private, add a GitHub PAT with `read:packages` under service → Settings → Registry credentials
4. **Environment variables** — all bot secrets live here (never in git): `DISCORD_TOKEN`, `GITHUB_PAT`, channel/role IDs, Plaky keys, etc.
5. **Port** — Render sets `PORT`; the bot reads `PORT` or `WEBHOOK_PORT` (default `8080`). Ensure the service listens on `$PORT` if Render assigns something other than 8080.
6. **Deploy hook** — Settings → Deploy Hook → copy the URL (see GitHub secret below)

Plaky webhooks should target your Render URL: `https://<your-service>.onrender.com/plaky/webhook`

## GitHub secret (required for CD)

| Secret | Where to get it |
|--------|-----------------|
| `RENDER_DEPLOY_HOOK_URL` | Render → Norozo service → **Settings** → **Deploy Hook** → copy URL |

Add under: GitHub repo → Settings → Secrets and variables → Actions → New repository secret.

Optional:

| Secret | Purpose |
|--------|---------|
| `DISCORD_WEBHOOK_URL` | Deploy notifications in Discord (already on this repo) |

## Manual deploy

Render dashboard → **Manual Deploy**, or trigger the deploy hook:

```bash
curl -X POST "$RENDER_DEPLOY_HOOK_URL"
```

To deploy a specific image tag:

```bash
IMG="ghcr.io/team-deepiri/deepiri-norozo:sha-abc1234"
ENC="$(python -c "import urllib.parse; print(urllib.parse.quote('$IMG', safe=''))")"
curl -X POST "${RENDER_DEPLOY_HOOK_URL}?imgURL=${ENC}"
```

## Troubleshooting

**CD pushes image but Render stays on old version** — `RENDER_DEPLOY_HOOK_URL` missing or wrong. Check Actions logs for the warning.

**Deploy hook 404** — `imgURL` registry path must match the image URL configured on the Render service (same registry, org, repo name).

**GHCR pull failed on Render** — add registry credentials; PAT needs `read:packages`.

**`CommandNotFound` / duplicate slash commands** — two instances running with the same Discord token. Scale Render to one instance or stop any local/manual copy.

**Plaky webhooks not arriving** — use the public Render URL; service must be awake (free tier spins down).
