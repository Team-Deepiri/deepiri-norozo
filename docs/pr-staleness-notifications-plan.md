# PR staleness notifications — design plan

Status: **plan only, not implemented**. Netcup SMTP unblock + `deepiri-proxy` SMTP
tunnel land first; this is next after that.

## Goal

Stale, unattended PRs across the Team-Deepiri org should surface automatically,
escalating in visibility as they age — without ever feeling like public shaming
at the low end. The nudge starts private (a DM) and only becomes public once a
PR has been sitting long enough that visibility is actually warranted.

## Escalation tiers

| PR age | Action | Visibility |
|---|---|---|
| ≥ 14 days (2 weeks) | Post to `#qa-support-team` | Team-visible, not public, not a personal callout |
| ≥ 17.5 days (2.5 weeks) | DM the PR author directly | Private — a respectful individual nudge |
| ≥ 30 days (1 month) | Post to `#announcements`, red embed, marked urgent | Public — by this point visibility is warranted, not shaming |

Each tier fires **once per PR** — a durable state table (see below) tracks which
tiers have already been notified so a periodic scan never re-sends the same
nudge on every run, and never re-escalates a tier that already fired.

## Identity resolution: GitHub PR author → Discord account

This is the hard part and the reason it's the identity-matching skill was
brought up. Reuses everything already built this session — no new matching
primitives, just a new *direction* through the existing chain, going through
Plaky as an intermediate hop when GitHub alone isn't enough:

```
GitHub PR author (login)
   │
   ├─ 1. Reverse-check the persisted github_username_map
   │     (built by kick-out's identity resolution + onboarding DM's github-link
   │     capture) -- if some discord_id already maps to this login, done.
   │
   ├─ 2. get_user_profile(login) -> real name (e.g. "Ricardo Beale")
   │     Fuzzy-match that name against current guild members' display_name/
   │     global_name/name via identity_match.best_match (same refuse-rather-
   │     than-guess philosophy already in use everywhere else). If confident,
   │     done -- and persist the mapping so this resolves instantly next time.
   │
   ├─ 3. Plaky hop for more context: find_user_email([login, real_name], ...)
   │     -- if Plaky has this person under a self-reported email, reverse-look
   │     that email up against the member_emails table (self-reported at
   │     onboarding) to land on a discord_id directly. This is the "GitHub to
   │     Plaky to Discord" path -- Plaky's roster sometimes has a cleaner
   │     display name than either GitHub or Discord alone.
   │
   └─ 4. No confident match anywhere -> log it, skip the DM tier, but the
        #qa-support-team and #announcements tiers still fire untagged (they're
        about the PR, not the person, when identity can't be resolved).
```

Needs one new platform capability: **reverse email lookup** on the existing
`member_emails` table (`GET .../member-email?email=...` alongside the existing
`?discord_id=...` lookup) -- everything else in this chain already exists.

## Data model

**New table on platform.deepiri.com** (same signed-webhook pattern as
`bot_state`/`member_emails`):

```sql
pr_staleness_state (
  repo TEXT NOT NULL,
  pr_number INT NOT NULL,
  notified_2week BOOLEAN NOT NULL DEFAULT false,
  notified_2_5week BOOLEAN NOT NULL DEFAULT false,
  notified_1month BOOLEAN NOT NULL DEFAULT false,
  resolved_discord_id TEXT,          -- cached once identity resolution succeeds
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (repo, pr_number)
)
```

Routes: `POST`/`GET /api/webhooks/norozo/pr-staleness` (upsert / lookup by
`repo`+`pr_number`), same HMAC scheme as everything else.

## Scan job

- Runs on an interval (candidate: every 6 hours — PR age changes slowly, no
  need for the 5-minute cadence used elsewhere).
- Enumerates open PRs across the whole org via `GET /orgs/Team-Deepiri/repos`
  (paginated, mirrors `list_org_members`'s pagination pattern) then
  `GET /repos/{repo}/pulls?state=open` per repo — or a single
  `GET /search/issues?q=org:Team-Deepiri+is:pr+is:open` call if the token's
  rate limit budget prefers fewer requests (worth checking actual PR volume
  before deciding).
- For each PR: compute age from `created_at`, look up its `pr_staleness_state`
  row, and fire whichever tier(s) just crossed their threshold and haven't
  been notified yet.
- Identity resolution (the chain above) only runs once per PR, lazily, the
  first time a tier needs to DM someone — cached in `resolved_discord_id`
  after that.

## Messages (draft wording, tune before shipping)

- **2-week (`#qa-support-team`)**: "PR #{number} in {repo} (\"{title}\") has been open 2 weeks: {url}" — plain, informational, no urgency framing.
- **2.5-week (DM)**: "Hey — your PR #{number} in {repo} (\"{title}\") has been open about 2.5 weeks. No pressure, just a nudge to take a look when you get a chance: {url}"
- **1-month (`#announcements`)**: red embed, title "PR open over 1 month", body naming the repo/PR/title/link, `@mention` the resolved author if identity resolution succeeded (by this point it's genuinely overdue and needs eyes, not anonymous).

## New env vars (copy-paste into Render)

```
PR_STALE_QA_CHANNEL_ID=1438705614649032755
PR_STALE_ANNOUNCE_CHANNEL_ID=1436509524818395156
```

`PR_STALE_QA_CHANNEL_ID` matches the existing `QA_CHANNEL_ID` value already in
your env — given as its own var so this feature's config is decoupled from
whatever `QA_CHANNEL_ID` is used for elsewhere (either can change independently
without affecting the other). `PR_STALE_ANNOUNCE_CHANNEL_ID` matches the
existing announcements channel — reusing the same channel Norozo already
posts to, not a new one.

No new channel is needed for the 2.5-week tier since that's a DM, not a
channel post.

## Open questions before implementation

1. **Which repos count?** All of Team-Deepiri, or an allowlist/denylist (e.g.
   skip forks, archived repos, or infra-only repos with no human PR flow)?
2. **Draft PRs** — exempt from the clock, or counted from creation regardless
   of draft status?
3. **Exact GitHub scan cost** — worth a quick check of current org-wide open
   PR count before committing to per-repo vs. search-API scanning, so the
   6-hour interval doesn't burn an unreasonable rate-limit budget.
4. Confirm the message wording above before it starts firing for real.
