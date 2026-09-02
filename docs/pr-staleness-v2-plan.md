# PR staleness v2: recurring DM cadence + QA reviewer nagging

## What's changing from v1 (already shipped)

v1 fired each of 3 tiers exactly once per PR: 2wk -> #qa-support-team,
2.5wk -> DM author, 1mo -> #announcements. This revises the DM behavior and
enriches the QA-channel post; the #announcements tier is untouched (still
fires exactly once, ever -- confirmed correct, no change).

## New behavior

**Author DM -- recurring, not one-time.** Once a PR crosses 14 days old, DM
the author on a cadence that tightens as it gets older, instead of a single
2.5-week ping:
- 14-20 days old: every 7 days
- 21-29 days old: every 3 days
- 30+ days old: every day

Tracked via `last_author_dm_at` (ISO timestamp) per PR. On each 6-hour scan,
compute the cadence for the PR's current age and DM again only if that many
days have elapsed since the last DM.

**#qa-support-team post -- still one-time at 14 days**, but now includes the
assigned QA reviewer: GitHub's requested reviewers, filtered to whoever holds
the QA Discord role (`QA_ROLE_ID`, reusing the existing env var already used
for GitHub-team sync -- defaults to 1436492938229186603 per Job's Discord
role), fuzzy-resolved to a Discord mention via the same GitHub->Discord chain
already built for PR-author resolution (`_resolve_discord_member_for_github_login`,
which itself mirrors deepiri-boardman's Plaky hop). If no requested reviewer
resolves to someone with the QA role: "No QA assigned".

**QA reviewer DM -- new, recurring on the same cadence as the author DM.**
For each requested reviewer who holds the QA Discord role and has not yet
submitted a review (checked via the PR's reviews endpoint -- any review state
counts as "reviewed", they're off the hook once they've weighed in at all),
DM them too, once the PR is 14+ days old, on the same 7/3/1-day cadence,
tracked per-reviewer in `reviewer_dm_state` (JSON: github_login -> last DM
ISO timestamp).

**#announcements 1-month post -- unchanged, one-time only**, per explicit
instruction: never repeat it, no matter how far past a month a PR gets.

## Data model

`pr_staleness_state` table gains two columns:
- `last_author_dm_at TIMESTAMPTZ NULL`
- `reviewer_dm_state JSONB NOT NULL DEFAULT '{}'`

`notified_2week` / `notified_1month` / `resolved_discord_id` unchanged.

## New GitHub API calls

- `get_pull_request(org, repo, number, pat)` -- full PR object (Search API
  doesn't include `requested_reviewers`; need the real pulls endpoint).
- `get_pull_request_reviews(org, repo, number, pat)` -- to know who's already
  reviewed.

Both only called for PRs already >= 14 days old (a small subset of open PRs),
so this doesn't meaningfully increase API volume.

## Env vars

- `QA_ROLE_ID` -- already exists (GitHub-team sync), now doubles as "who
  counts as QA for staleness pings". Defaults to `1436492938229186603` if
  unset, so nothing to add in Render unless you want to override it.
- `PR_STALE_QA_CHANNEL_ID` / `PR_STALE_ANNOUNCE_CHANNEL_ID` -- unchanged,
  already default to the real channel IDs (1438705614649032755 /
  1436509524818395156), which is why they've worked without being set.
