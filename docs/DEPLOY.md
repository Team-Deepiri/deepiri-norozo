# Norozo production deployment (Render)

Norozo runs on [Render](https://render.com). Production is **Git-backed**: Render builds from this repo’s `Dockerfile` when deploy runs — it does not pull from GHCR today.

## What you saw in Render

A log like:

> Deploy live for **a7cfb21**: Merge pull request #24 …

means Render’s **GitHub auto-deploy** fired on the merge to `main`. That is separate from the GitHub Actions CD workflow. Production can be live even when the Actions “Deploy to Render” step fails.

## What CD does

On push to `main`:

1. Runs tests (pytest + ruff)
2. If tests pass → POSTs to `RENDER_DEPLOY_HOOK_URL` to trigger a Render deploy (builds from the repo commit)
3. In parallel after tests → builds and pushes `ghcr.io/team-deepiri/deepiri-norozo:latest` and `:sha-<short-sha>` (artifact registry; not used by Render unless you switch to image-backed deploy)

## Recommended Render settings (test-gated deploy)

To avoid deploying **before** tests pass:

1. Render → Norozo service → **Settings** → **Auto-Deploy** → **Off**
2. Keep `RENDER_DEPLOY_HOOK_URL` in GitHub Actions secrets
3. CD runs tests first, then hits the deploy hook — Render only deploys green builds

If auto-deploy stays **On**, every merge to `main` deploys immediately (what happened for `a7cfb21`), and the deploy hook causes a second deploy after tests.

## One-time setup

### Render

- **Source** — GitHub repo `Team-Deepiri/deepiri-norozo`, branch `main`
- **Runtime** — Docker (uses repo `Dockerfile`)
- **Environment variables** — `DISCORD_TOKEN`, `GITHUB_PAT`, channel/role IDs, Plaky keys, etc. (never in git)
- **Port** — Render sets `PORT`; the bot reads `PORT` or `WEBHOOK_PORT` (default `8080`)
- **Deploy hook** — Settings → Deploy Hook → copy URL → GitHub secret `RENDER_DEPLOY_HOOK_URL`
- **Persistent disk** — mount a disk at `/var/data` and set `PERSISTENT_DATA_DIR=/var/data`. GitHub username mappings and announcement idempotency records are stored there. Render's default filesystem is ephemeral and is not safe for these files.

Plaky webhooks: `https://<your-service>.onrender.com/plaky/webhook`

### Announcement bridge deployment contract

Deploy the platform/gateway endpoint before enabling outbound forwarding from Norozo.
Configure these values on both sides with the same randomly generated secret:

| Norozo variable | Requirement |
|-----------------|-------------|
| `ANNOUNCEMENTS_INBOUND_SECRET` | Required. Incoming platform announcements return `503` when unset and `401` for missing or invalid signatures. |
| `PLATFORM_ANNOUNCEMENTS_WEBHOOK_URL` | Platform/gateway endpoint that accepts Discord announcements. Leave unset until that endpoint is deployed. |
| `PLATFORM_ANNOUNCEMENTS_WEBHOOK_SECRET` | Required whenever the outbound URL is set; must match the gateway's verification secret. |
| `PERSISTENT_DATA_DIR` | Set to the Render persistent-disk mount (`/var/data`) for durable mappings and webhook deduplication. |

The gateway should send a stable `Idempotency-Key`, `X-Idempotency-Key`, `event_id`,
or `announcement_id` with retries. Norozo falls back to hashing the signed request
body when none is provided. Verify `/health` reports
`"announcement_webhook_ready": true` before directing gateway traffic to Norozo.

### GitHub secrets

| Secret | Purpose |
|--------|---------|
| `RENDER_DEPLOY_HOOK_URL` | Trigger Render deploy after tests pass |
| `DISCORD_WEBHOOK_URL` | Optional deploy notifications in Discord |

## Do not use `imgURL` on this service

`imgURL` is for **image-backed** Render services (pull prebuilt image from GHCR). This service is **Git-backed**. Passing `imgURL` returns **400**.

## Manual deploy

Render dashboard → **Manual Deploy**, or:

```bash
curl -X POST "$RENDER_DEPLOY_HOOK_URL"
```

## Troubleshooting

**Actions failed but Render shows “Deploy live”** — auto-deploy from GitHub ran; fix the Actions step separately.

**Deploy hook 400** — remove `imgURL`; use plain POST to the hook URL.

**Deploy hook skipped** — `RENDER_DEPLOY_HOOK_URL` not set; only auto-deploy (if enabled) will run.

**`CommandNotFound` / duplicate slash commands** — two bot instances share the same Discord token. Ensure only one Render instance is running.

**Plaky webhooks not arriving** — use the public Render URL; free tier spins down when idle.

## Future: image-backed deploy from GHCR

To have Render pull from GHCR instead of building from Git:

1. Create a new Render web service → **Existing Image** → `ghcr.io/team-deepiri/deepiri-norozo:latest`
2. Add GHCR registry credentials on Render (`read:packages` PAT)
3. Turn off the Git-backed service (or disable its auto-deploy)
4. CD already pushes to GHCR; deploy hook without `imgURL` pulls `:latest`

Only use `imgURL` if the image path on Render matches GHCR exactly.
