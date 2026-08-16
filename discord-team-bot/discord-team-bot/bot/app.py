import logging

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .database import Database, Team

log = logging.getLogger("team_bot")


def role_problem(guild: discord.Guild, role: discord.Role) -> str | None:
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return "I need the **Manage Roles** permission."
    if role >= me.top_role:
        return f"Move my bot role above {role.mention} in Server Settings → Roles."
    if role.managed:
        return "That role is managed by an integration and cannot be assigned manually."
    return None


class OfferView(discord.ui.View):
    def __init__(self, bot: "TeamBot", offer_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.offer_id = offer_id
        self.accept.custom_id = f"offer:{offer_id}:accept"
        self.deny.custom_id = f"offer:{offer_id}:deny"

    async def validate(self, interaction: discord.Interaction):
        offer = await self.bot.db.get_offer(self.offer_id)
        if not offer or offer.status != "pending":
            await interaction.response.send_message("This offer has already been resolved.", ephemeral=True)
            return None
        if interaction.user.id != offer.player_id:
            await interaction.response.send_message("Only the player who received this offer can answer it.", ephemeral=True)
            return None
        return offer

    def disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="offer:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        offer = await self.validate(interaction)
        if not offer:
            return
        guild = self.bot.get_guild(offer.guild_id)
        role = guild.get_role(offer.team_role_id) if guild else None
        member = guild.get_member(offer.player_id) if guild else None
        if not guild or not role or not member:
            await interaction.response.send_message("The server, team role, or your membership no longer exists.", ephemeral=True)
            return
        problem = role_problem(guild, role)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        try:
            await member.add_roles(role, reason=f"Accepted team offer #{offer.id}")
        except discord.Forbidden:
            await interaction.response.send_message("I could not add the role. Check my permissions and role position.", ephemeral=True)
            return
        if not await self.bot.db.resolve_offer(offer.id, "accepted"):
            await interaction.response.send_message("This offer was already answered.", ephemeral=True)
            return
        self.disable_all()
        await interaction.response.edit_message(content=f"You accepted the offer for **{role.name}**!", embed=None, view=self)
        await self.bot.notify_manager(offer.manager_id, f"<@{offer.player_id}> accepted the offer for **{role.name}**.")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="offer:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        offer = await self.validate(interaction)
        if not offer:
            return
        if not await self.bot.db.resolve_offer(offer.id, "denied"):
            await interaction.response.send_message("This offer was already answered.", ephemeral=True)
            return
        guild = self.bot.get_guild(offer.guild_id)
        role = guild.get_role(offer.team_role_id) if guild else None
        team_name = role.name if role else "the team"
        self.disable_all()
        await interaction.response.edit_message(content=f"You denied the offer for **{team_name}**.", embed=None, view=self)
        await self.bot.notify_manager(offer.manager_id, f"<@{offer.player_id}> denied the offer for **{team_name}**.")


class TeamBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.db = Database(config.database_path)

    async def setup_hook(self):
        if not self.config.auto_migrate:
            raise RuntimeError("AUTO_MIGRATE=false is not supported until the database has been migrated manually.")
        await self.db.migrate()
        for offer in await self.db.pending_offers():
            self.add_view(OfferView(self, offer.id), message_id=offer.message_id)
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s", self.config.guild_id)

    async def notify_manager(self, manager_id: int, message: str):
        try:
            user = self.get_user(manager_id) or await self.fetch_user(manager_id)
            await user.send(message)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.warning("Could not DM manager %s", manager_id)


config = Config.from_env()
bot = TeamBot(config)


def admin_allowed(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.user.id in config.owner_ids
        or (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator)
    )


@bot.tree.command(description="Configure a team and its manager")
@app_commands.describe(manager="The team's manager", team_role="The team's Discord role")
@app_commands.guild_only()
async def addteam(interaction: discord.Interaction, manager: discord.Member, team_role: discord.Role):
    if not admin_allowed(interaction):
        await interaction.response.send_message("Only a configured owner or server administrator can use this.", ephemeral=True)
        return
    manager_role = interaction.guild.get_role(config.manager_role_id)
    if manager_role is None:
        await interaction.response.send_message("MANAGER_ROLE_ID does not match a role in this server.", ephemeral=True)
        return
    for role in (manager_role, team_role):
        if problem := role_problem(interaction.guild, role):
            await interaction.response.send_message(problem, ephemeral=True)
            return
    try:
        await manager.add_roles(manager_role, team_role, reason=f"Team configured by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("I could not assign the roles. Check Manage Roles and role hierarchy.", ephemeral=True)
        return
    await bot.db.upsert_team(Team(interaction.guild.id, team_role.id, manager.id))
    await interaction.response.send_message(
        f"Configured {team_role.mention} with {manager.mention} as manager.", ephemeral=True
    )


@bot.tree.command(description="Send a player an offer to join your team")
@app_commands.describe(player="Player receiving the offer", team_role="Your configured team")
@app_commands.guild_only()
async def offer(interaction: discord.Interaction, player: discord.Member, team_role: discord.Role):
    team = await bot.db.get_team(interaction.guild.id, team_role.id)
    if not team:
        await interaction.response.send_message("That role is not a configured team. Ask an admin to use /addteam.", ephemeral=True)
        return
    if interaction.user.id != team.manager_id:
        await interaction.response.send_message("Only this team's configured manager can send its offers.", ephemeral=True)
        return
    if player.bot or player.id == interaction.user.id:
        await interaction.response.send_message("Choose another human member as the player.", ephemeral=True)
        return
    if team_role in player.roles:
        await interaction.response.send_message("That player already has this team role.", ephemeral=True)
        return
    if problem := role_problem(interaction.guild, team_role):
        await interaction.response.send_message(problem, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    offer_id = await bot.db.create_offer(interaction.guild.id, team_role.id, interaction.user.id, player.id)
    embed = discord.Embed(
        title="Team offer",
        description=f"{interaction.user.mention} has invited you to join **{team_role.name}**.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Server: {interaction.guild.name} • Offer #{offer_id}")
    try:
        message = await player.send(embed=embed, view=OfferView(bot, offer_id))
    except discord.Forbidden:
        await bot.db.resolve_offer(offer_id, "dm_failed")
        await interaction.followup.send("I could not DM that player. They may have server DMs turned off.", ephemeral=True)
        return
    except discord.HTTPException:
        await bot.db.resolve_offer(offer_id, "dm_failed")
        await interaction.followup.send("Discord could not deliver the DM. Please try again.", ephemeral=True)
        return
    await bot.db.set_offer_message(offer_id, message.id)
    await interaction.followup.send(f"Offer sent to {player.mention}.", ephemeral=True)


@bot.tree.command(description="Remove a player from your team")
@app_commands.describe(player="Current team member", team_role="Your configured team")
@app_commands.guild_only()
async def release(interaction: discord.Interaction, player: discord.Member, team_role: discord.Role):
    team = await bot.db.get_team(interaction.guild.id, team_role.id)
    if not team:
        await interaction.response.send_message("That role is not a configured team.", ephemeral=True)
        return
    if interaction.user.id != team.manager_id:
        await interaction.response.send_message("Only this team's configured manager can release its players.", ephemeral=True)
        return
    if team_role not in player.roles:
        await interaction.response.send_message("That member does not currently have this team role.", ephemeral=True)
        return
    if problem := role_problem(interaction.guild, team_role):
        await interaction.response.send_message(problem, ephemeral=True)
        return
    try:
        await player.remove_roles(team_role, reason=f"Released by team manager {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("I could not remove that role. Check my permissions and role position.", ephemeral=True)
        return
    await interaction.response.send_message(f"Removed {team_role.mention} from {player.mention}. Only that team role was removed.", ephemeral=True)


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Slash command failed", exc_info=error)
    message = "Something went wrong. Check the Railway logs for details."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def run():
    logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO))
    bot.run(config.token, log_handler=None)
