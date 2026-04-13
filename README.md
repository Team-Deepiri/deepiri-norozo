# Discord to GitHub Discussions Bridge

Bridges messages from a Discord announcements channel into GitHub Discussions.

When a message is posted in Discord channel announcements (or the channel configured by DISCORD_CHANNEL_ID), the bot creates a new discussion under your chosen GitHub Discussions category and reacts back on Discord:

- ✅ success
- ❌ failure

## Requirements

- Python 3.11+
- A GitHub repository with Discussions enabled
- A GitHub Personal Access Token with repo scope (or public_repo for public repos)
- A Discord bot token with intents: guilds and message_content

## 1) Create GitHub PAT

1. Go to GitHub Settings -> Developer settings -> Personal access tokens.
2. Create a token with:
	- repo (private repos), or
	- public_repo (public repos)
3. Put it in env as GITHUB_PAT.

## 2) Environment Variables

Copy .env.example to .env and fill values:

- GITHUB_PAT
- GITHUB_OWNER
- GITHUB_REPO
- DISCORD_BOT_TOKEN
- DISCORD_CHANNEL_ID (optional but recommended)
- DISCORD_CHANNEL_NAME (fallback if channel id not set, default announcements)

You can also let setup.py populate IDs:

- REPO_ID
- CATEGORY_ID
- GITHUB_REPO_ID
- GITHUB_CATEGORY_ID

## 3) Install Dependencies

pip install -r requirements.txt

## 4) Run setup.py to fetch repository/category IDs

This script calls GitHub GraphQL to fetch repo id and discussion categories, prints categories, selects the target category, and updates .env.

Example:

python setup.py --owner Team-Deepiri --repo deepiri-norozo --category Announcements

What it does:

1. Fetches repository node id
2. Fetches discussion categories
3. Prints category names and ids
4. Writes IDs into .env

GraphQL query used:

query { repository(owner: "OWNER", name: "REPO") { id, discussionCategories(first: 10) { nodes { id, name } } } }

## 5) Run the bot

python bot.py

Behavior:

1. Listens only to target channel
2. Ignores bot messages
3. Creates title from first line or first 60 chars
4. Builds body with full message + author + timestamp
5. Calls GitHub GraphQL createDiscussion mutation
6. Reacts with ✅ or ❌

## 6) Run tests

pytest -q

Tests included:

- tests/test_setup.py
- tests/test_discussion.py
- tests/test_bot.py

## Docker

Build and run with docker-compose:

docker-compose up --build

The service reads secrets from .env and runs python bot.py.
