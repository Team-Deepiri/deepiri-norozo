import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
import pytz
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks


EST = pytz.timezone("US/Eastern")
UTC = pytz.utc
DEFAULT_STORAGE_PATH = Path(__file__).with_name("meetings.json")
WEEKLY_MEETING_RULES = {
    "ai/ml": {"weekday": 0, "hour": 21, "minute": 30},
    "qa": {"weekday": 0, "hour": 22, "minute": 0},
    "frontend & backend & infrastructure": {"weekday": 1, "hour": 21, "minute": 0},
}


class MeetingReminderService:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.storage_path = Path(os.getenv("MEETINGS_FILE", str(DEFAULT_STORAGE_PATH)))
        self.announcements_channel_id = self._int_env("ANNOUNCEMENTS_CHANNEL_ID")
        self.staff_role_id = self._int_env("STAFF_ROLE_ID") or self._int_env("MEETING_STAFF_ROLE_ID")
        self._lock = asyncio.Lock()
        self._ensure_storage_file()

    @staticmethod
    def _int_env(name: str) -> Optional[int]:
        value = os.getenv(name)
        if value is None or value == "":
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _ensure_storage_file(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_path.exists():
            self.storage_path.write_text("[]", encoding="utf-8")

    async def _read_meetings(self) -> List[Dict[str, Any]]:
        async with self._lock:
            self._ensure_storage_file()
            try:
                raw = self.storage_path.read_text(encoding="utf-8").strip() or "[]"
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    return []

                meetings: List[Dict[str, Any]] = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    utc_time = item.get("utc_time")
                    if isinstance(name, str) and isinstance(utc_time, str):
                        meetings.append({"name": name, "utc_time": utc_time})
                return meetings
            except (json.JSONDecodeError, OSError):
                return []

    async def _write_meetings(self, meetings: List[Dict[str, Any]]) -> None:
        async with self._lock:
            self._ensure_storage_file()
            self.storage_path.write_text(json.dumps(meetings, indent=2), encoding="utf-8")

    @staticmethod
    def _format_est(dt_utc: datetime, include_year: bool = False) -> str:
        dt_est = dt_utc.astimezone(EST)
        month = dt_est.strftime("%B")
        day = dt_est.day
        year = dt_est.year
        hour_12 = dt_est.strftime("%I").lstrip("0") or "12"
        minute = dt_est.strftime("%M")
        am_pm = dt_est.strftime("%p")
        if include_year:
            return f"{month} {day}, {year} at {hour_12}:{minute} {am_pm} EST"
        return f"{month} {day} at {hour_12}:{minute} {am_pm} EST"

    def _member_is_staff(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False

        member = interaction.user
        if member.guild_permissions.administrator:
            return True

        for role in member.roles:
            if role.name == "Staff":
                return True
            if self.staff_role_id is not None and role.id == self.staff_role_id:
                return True

        return False

    @staticmethod
    def _normalized_name(name: str) -> str:
        return " ".join(name.strip().lower().split())

    def _is_weekly_meeting(self, name: str) -> bool:
        return self._normalized_name(name) in WEEKLY_MEETING_RULES

    async def _append_if_missing(self, meetings: List[Dict[str, Any]], name: str, utc_dt: datetime) -> List[Dict[str, Any]]:
        serialized = utc_dt.replace(tzinfo=None).isoformat(timespec="seconds")
        exists = any(
            str(m.get("name", "")).strip().lower() == name.strip().lower() and m.get("utc_time") == serialized
            for m in meetings
        )
        if not exists:
            meetings.append({"name": name, "utc_time": serialized})
            meetings.sort(key=self._meeting_sort_key)
        return meetings

    @staticmethod
    def _meeting_sort_key(meeting: Dict[str, Any]) -> datetime:
        try:
            return datetime.fromisoformat(str(meeting["utc_time"]))
        except Exception:
            return datetime.max

    @tasks.loop(minutes=1)
    async def reminder_loop(self) -> None:
        if self.announcements_channel_id is None:
            return

        channel = self.bot.get_channel(self.announcements_channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched = await self.bot.fetch_channel(self.announcements_channel_id)
                if isinstance(fetched, discord.TextChannel):
                    channel = fetched
                else:
                    return
            except discord.DiscordException:
                return

        meetings = await self._read_meetings()
        if not meetings:
            return

        now_utc = datetime.now(UTC)
        one_minute = timedelta(minutes=1)
        thirty_minutes = timedelta(minutes=30)

        remaining_meetings: List[Dict[str, Any]] = []
        any_changed = False

        for meeting in meetings:
            try:
                meeting_utc = datetime.fromisoformat(meeting["utc_time"]).replace(tzinfo=UTC)
            except Exception:
                any_changed = True
                continue

            delta = meeting_utc - now_utc

            if timedelta(0) <= (delta - thirty_minutes) < one_minute:
                await channel.send(f"@everyone 🔔 {meeting['name']} starts in 30 minutes!")

            if timedelta(0) <= delta < one_minute:
                await channel.send(f"@everyone 🚨 {meeting['name']} is starting now!")
                if self._is_weekly_meeting(meeting["name"]):
                    next_week = meeting_utc + timedelta(days=7)
                    remaining_meetings = await self._append_if_missing(remaining_meetings, meeting["name"], next_week)
                any_changed = True
                continue

            if delta >= timedelta(0):
                remaining_meetings.append(meeting)
            else:
                any_changed = True

        if any_changed:
            await self._write_meetings(remaining_meetings)

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    def start_loop(self) -> None:
        if not self.reminder_loop.is_running():
            self.reminder_loop.start()

    def register_commands(self) -> None:
        @self.bot.tree.command(name="schedule-meeting", description="Schedule a team meeting")
        @app_commands.describe(
            meeting_name="Name of the meeting, e.g. AI Team Meeting",
            date="Date in YYYY-MM-DD (EST)",
            time="Time in HH:MM 24-hour format (EST)",
        )
        async def schedule_meeting(
            interaction: discord.Interaction,
            meeting_name: str,
            date: str,
            time: str,
        ) -> None:
            if not self._member_is_staff(interaction):
                await interaction.response.send_message(
                    "Only staff or admins can use this command.",
                    ephemeral=True,
                )
                return

            try:
                naive_est = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
                meeting_est = EST.localize(naive_est, is_dst=None)
            except (pytz.NonExistentTimeError, pytz.AmbiguousTimeError):
                await interaction.response.send_message(
                    "That local EST time is invalid/ambiguous due to daylight saving time. Please choose another time.",
                    ephemeral=True,
                )
                return
            except ValueError:
                await interaction.response.send_message(
                    "Invalid date/time. Use date as YYYY-MM-DD and time as HH:MM (24-hour EST).",
                    ephemeral=True,
                )
                return

            meeting_utc = meeting_est.astimezone(UTC)
            if meeting_utc <= datetime.now(UTC):
                await interaction.response.send_message(
                    "Meeting time must be in the future.",
                    ephemeral=True,
                )
                return

            meetings = await self._read_meetings()
            before_count = len(meetings)
            meetings = await self._append_if_missing(meetings, meeting_name, meeting_utc)
            if len(meetings) == before_count:
                await interaction.response.send_message(
                    "A meeting with that name and time is already scheduled.",
                    ephemeral=True,
                )
                return

            await self._write_meetings(meetings)

            embed = discord.Embed(
                title="Meeting Scheduled",
                description=f"{meeting_name} — {self._format_est(meeting_utc, include_year=True)}",
                color=discord.Color.green(),
            )
            await interaction.response.send_message(embed=embed)

        @self.bot.tree.command(name="list-meetings", description="List all upcoming meetings")
        async def list_meetings(interaction: discord.Interaction) -> None:
            meetings = await self._read_meetings()

            upcoming: List[Dict[str, Any]] = []
            now_utc = datetime.now(UTC)
            for meeting in meetings:
                try:
                    meeting_utc = datetime.fromisoformat(meeting["utc_time"]).replace(tzinfo=UTC)
                except Exception:
                    continue

                if meeting_utc >= now_utc:
                    upcoming.append(meeting)

            upcoming.sort(key=self._meeting_sort_key)

            if not upcoming:
                await interaction.response.send_message("No upcoming meetings.")
                return

            lines: List[str] = []
            for meeting in upcoming:
                meeting_utc = datetime.fromisoformat(meeting["utc_time"]).replace(tzinfo=UTC)
                lines.append(f"{meeting['name']} — {self._format_est(meeting_utc)}")

            embed = discord.Embed(
                title="Upcoming Meetings",
                description="\n".join(lines),
                color=discord.Color.blue(),
            )
            await interaction.response.send_message(embed=embed)

        @self.bot.tree.command(name="cancel-meeting", description="Cancel a meeting by name")
        @app_commands.describe(meeting_name="Meeting name to cancel")
        async def cancel_meeting(interaction: discord.Interaction, meeting_name: str) -> None:
            if not self._member_is_staff(interaction):
                await interaction.response.send_message(
                    "Only staff or admins can use this command.",
                    ephemeral=True,
                )
                return

            meetings = await self._read_meetings()
            now_utc = datetime.now(UTC)

            indexed_matches: List[tuple[int, datetime]] = []
            for i, meeting in enumerate(meetings):
                if str(meeting.get("name", "")).strip().lower() != meeting_name.strip().lower():
                    continue
                try:
                    meeting_utc = datetime.fromisoformat(meeting["utc_time"]).replace(tzinfo=UTC)
                except Exception:
                    continue
                if meeting_utc >= now_utc:
                    indexed_matches.append((i, meeting_utc))

            if not indexed_matches:
                await interaction.response.send_message("No meeting found with that name.", ephemeral=True)
                return

            indexed_matches.sort(key=lambda item: item[1])
            remove_index = indexed_matches[0][0]
            removed = meetings.pop(remove_index)

            if self._is_weekly_meeting(removed["name"]):
                removed_utc = datetime.fromisoformat(removed["utc_time"]).replace(tzinfo=UTC)
                next_week = removed_utc + timedelta(days=7)
                meetings = await self._append_if_missing(meetings, removed["name"], next_week)

            await self._write_meetings(meetings)
            await interaction.response.send_message(f"{removed['name']} has been cancelled.")


def setup_meeting_features(bot: commands.Bot) -> MeetingReminderService:
    service = MeetingReminderService(bot)
    service.register_commands()
    return service
