import discord
import re


class ApprovalView(discord.ui.View):
    MENTION_RE = re.compile(r"<@!?(\d+)>")

    def __init__(self, dev_team_role_id: int, available_role_id: int):
        super().__init__(timeout=None)
        self.dev_team_role_id = dev_team_role_id
        self.available_role_id = available_role_id

    def _extract_target_user_id(self, interaction: discord.Interaction) -> int | None:
        message = interaction.message
        if not message or not message.embeds:
            return None

        description = message.embeds[0].description or ""
        match = self.MENTION_RE.search(description)
        if not match:
            return None

        try:
            return int(match.group(1))
        except ValueError:
            return None

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="ipca_approve")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.guild or not interaction.user:
            await interaction.response.send_message("This action must be used in a server.", ephemeral=True)
            return

        target_user_id = self._extract_target_user_id(interaction)
        if target_user_id is None:
            await interaction.response.send_message(
                "Could not determine who to approve from this request message.",
                ephemeral=True,
            )
            return

        clicker = interaction.user
        is_staff = isinstance(clicker, discord.Member) and (
            clicker.guild_permissions.manage_roles or clicker.guild_permissions.administrator
        )
        if not is_staff:
            await interaction.response.send_message("You do not have permission to approve this request.", ephemeral=True)
            return

        dev_role = interaction.guild.get_role(self.dev_team_role_id)
        available_role = interaction.guild.get_role(self.available_role_id)
        if dev_role is None or available_role is None:
            await interaction.response.send_message(
                "Configured Available/DEV team roles were not found.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(target_user_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(target_user_id)
            except discord.NotFound:
                member = None

        if member is None:
            await interaction.response.send_message("Target user is no longer in the server.", ephemeral=True)
            return

        try:
            await member.add_roles(available_role, dev_role, reason="IPCA approved by staff")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to assign these roles. Check role hierarchy and permissions.",
                ephemeral=True,
            )
            return

        if interaction.message:
            # Disable this specific message button without mutating the shared persistent view.
            disabled_view = discord.ui.View(timeout=None)
            disabled_view.add_item(
                discord.ui.Button(
                    label="Approve",
                    style=discord.ButtonStyle.success,
                    custom_id="ipca_approve",
                    disabled=True,
                )
            )
            await interaction.message.edit(view=disabled_view)

        await interaction.response.send_message(
            f"Approved. {member.mention} has been assigned {available_role.mention} and {dev_role.mention}."
        )
