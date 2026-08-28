import logging
import os
import random
import time
from datetime import timezone
from typing import Awaitable, Callable

import discord
from discord.ext import commands
from dotenv import load_dotenv

from github_discussion import GitHubDiscussionError, create_github_discussion


logger = logging.getLogger(__name__)


def _int_env(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _first_int_env(*names: str) -> int | None:
    for name in names:
        value = _int_env(name)
        if value is not None:
            return value
    return None


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_discord_rate_limited(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    if status == 429:
        return True

    message = str(exc).lower()
    return "429" in message or "too many requests" in message or "rate limited" in message


def _run_discord_bot(
    bot: commands.Bot,
    token: str,
    *,
    max_attempts: int = 4,
    base_delay: float = 5.0,
) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            bot.run(token)
            return
        except Exception as exc:  # pragma: no cover - exercised via rate-limit retry handling
            if not _is_discord_rate_limited(exc):
                raise

            if attempt == max_attempts:
                logger.warning(
                    "Discord rate limited; exhausted retries after %s attempts.",
                    max_attempts,
                )
                raise

            delay = base_delay * (2 ** (attempt - 1))
            jitter = random.uniform(0, delay * 0.25)
            sleep_for = delay + jitter
            logger.warning(
                "Discord rate limited (HTTP 429); retrying in %.1f seconds (attempt %s/%s)",
                sleep_for,
                attempt,
                max_attempts,
            )
            time.sleep(sleep_for)


def format_discussion_title(message_content: str) -> str:
    text = (message_content or "").strip()
    if not text:
        return "Announcement"

    first_line = text.splitlines()[0].strip()
    if len(first_line) <= 60:
        return first_line
    return first_line[:57].rstrip() + "..."


def format_discussion_body(message: discord.Message) -> str:
    created = message.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    author = f"{message.author}"
    content = (message.content or "").strip()
    if not content and message.attachments:
        content = "\n".join(attachment.url for attachment in message.attachments)

    return (
        f"{content}\n\n"
        f"---\n"
        f"Posted by: {author}\n"
        f"Discord message ID: {message.id}\n"
        f"Timestamp (UTC): {created.astimezone(timezone.utc).isoformat()}"
    )


class DiscussionsBridgeBot(commands.Bot):
    def __init__(self, enable_message_content_intent: bool | None = None) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        if enable_message_content_intent is None:
            enable_message_content_intent = _bool_env("DISCORD_ENABLE_MESSAGE_CONTENT_INTENT", True)
        intents.message_content = enable_message_content_intent

        super().__init__(command_prefix="!", intents=intents)

        self.message_content_enabled = enable_message_content_intent
        self.target_channel_id = _first_int_env("DISCORD_CHANNEL_ID", "ANNOUNCEMENTS_CHANNEL_ID")
        self.target_channel_name = os.getenv("DISCORD_CHANNEL_NAME", "announcements")

    def _is_target_channel(self, message: discord.Message) -> bool:
        if self.target_channel_id is not None:
            return message.channel.id == self.target_channel_id

        channel_name = getattr(message.channel, "name", None)
        return channel_name == self.target_channel_name

    async def process_bridge_message(
        self,
        message: discord.Message,
        create_discussion_fn: Callable[[str, str], Awaitable[str]] = create_github_discussion,
    ) -> None:
        if message.author.bot:
            logger.debug("Ignoring bot message %s", message.id)
            return

        if not self._is_target_channel(message):
            logger.debug("Ignoring message %s from non-target channel", message.id)
            return

        title = format_discussion_title(message.content)
        body = format_discussion_body(message)

        try:
            discussion_url = await create_discussion_fn(title, body)
            logger.info("Bridged Discord message %s -> %s", message.id, discussion_url)
            await message.add_reaction("✅")
        except GitHubDiscussionError as exc:
            logger.error("Bridge failed for message %s: %s", message.id, exc)
        except Exception as exc:  # pragma: no cover
            logger.exception("Unexpected bridge error for message %s: %s", message.id, exc)

    async def on_ready(self) -> None:
        logger.info("Logged in as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        await self.process_bridge_message(message)


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = (os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Missing Discord token in environment (set DISCORD_BOT_TOKEN or DISCORD_TOKEN).")

    enable_message_content = _bool_env("DISCORD_ENABLE_MESSAGE_CONTENT_INTENT", True)

    bot = DiscussionsBridgeBot(enable_message_content_intent=enable_message_content)
    try:
        _run_discord_bot(bot, token)
        return
    except discord.errors.PrivilegedIntentsRequired:
        if not enable_message_content:
            raise

        logger.warning(
            "Message Content intent is not enabled in Discord Developer Portal. "
            "Falling back with DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=false. "
            "Enable Message Content intent for full message mirroring."
        )

    fallback_bot = DiscussionsBridgeBot(enable_message_content_intent=False)
    _run_discord_bot(fallback_bot, token)


if __name__ == "__main__":
    main()
