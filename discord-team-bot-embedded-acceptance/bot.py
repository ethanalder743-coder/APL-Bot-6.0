import os
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MANAGER_ROLE_ID = int(os.getenv("MANAGER_ROLE_ID", "0"))
SIGNINGS_CHANNEL_ID = int(os.getenv("SIGNINGS_CHANNEL_ID", "0"))
OWNER_IDS = {
    int(value.strip())
    for value in os.getenv("OWNER_IDS", "").split(",")
    if value.strip().isdigit()
}
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/teams.db"))
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)


def connect_db():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def setup_database():
    with connect_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS teams (
                team_role_id INTEGER PRIMARY KEY,
                manager_id INTEGER NOT NULL
            )
            """
        )
        # Older versions allowed one manager to be connected to several teams.
        # Keep only their most recently added team before enforcing one team each.
        db.execute(
            """
            DELETE FROM teams
            WHERE rowid NOT IN (
                SELECT MAX(rowid) FROM teams GROUP BY manager_id
            )
            """
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_team_per_manager ON teams(manager_id)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS offers (
                message_id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL,
                team_role_id INTEGER NOT NULL,
                manager_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )


def get_team_for_manager(manager_id):
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM teams WHERE manager_id = ?", (manager_id,)
        ).fetchone()


def get_offer(message_id):
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM offers WHERE message_id = ?", (message_id,)
        ).fetchone()


def finish_offer(message_id, status):
    with connect_db() as db:
        db.execute(
            "UPDATE offers SET status = ? WHERE message_id = ? AND status = 'pending'",
            (status, message_id),
        )


intents = discord.Intents.default()
intents.members = True


class TeamBot(commands.Bot):
    async def setup_hook(self):
        setup_database()
        # Restore buttons for offers that were waiting when the bot restarted.
        with connect_db() as db:
            pending = db.execute(
                "SELECT message_id FROM offers WHERE status = 'pending'"
            ).fetchall()
        for offer in pending:
            self.add_view(OfferView(offer["message_id"]), message_id=offer["message_id"])

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = TeamBot(command_prefix="!", intents=intents)


async def reply(interaction, message, *, ephemeral=True):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(message, ephemeral=ephemeral)


def is_bot_owner(user_id):
    return user_id in OWNER_IDS


class OfferView(discord.ui.View):
    def __init__(self, message_id=None):
        super().__init__(timeout=None)
        self.message_id = message_id

    async def check_offer(self, interaction):
        message_id = self.message_id or interaction.message.id
        offer = get_offer(message_id)
        if not offer or offer["status"] != "pending":
            await reply(interaction, "This offer is no longer active.")
            return None
        if interaction.user.id != offer["player_id"]:
            await reply(interaction, "This offer belongs to another player.")
            return None
        return offer

    async def close_buttons(self, interaction, *, embed=None):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(embed=embed, view=self)

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id="team_offer:accept",
    )
    async def accept(self, interaction, button):
        offer = await self.check_offer(interaction)
        if not offer:
            return

        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            await reply(interaction, "I cannot find the configured server. Please contact an admin.")
            return

        member = guild.get_member(offer["player_id"])
        role = guild.get_role(offer["team_role_id"])
        if member is None or role is None:
            await reply(interaction, "The player or team role could not be found. Please contact an admin.")
            return

        try:
            await member.add_roles(role, reason="Player accepted a team offer")
        except discord.Forbidden:
            await reply(interaction, "I cannot assign that role. Put my bot role above the team role.")
            return

        finish_offer(interaction.message.id, "accepted")
        await interaction.response.defer()
        accepted_embed = discord.Embed(
            title="Offer Accepted",
            description=f"You accepted the offer for {role.mention}!",
            color=discord.Color.green(),
        )
        accepted_embed.add_field(name="Team", value=role.mention, inline=True)
        accepted_embed.add_field(
            name="Manager / Franchise Owner",
            value=f"<@{offer['manager_id']}>",
            inline=True,
        )
        accepted_embed.set_footer(text="Your team role has been assigned")
        await self.close_buttons(interaction, embed=accepted_embed)

        # No additional DM or public acceptance text is sent here. The original
        # offer DM is edited into the embed above, and the signing channel gets
        # the separate announcement embed below.
        channel = guild.get_channel(SIGNINGS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(SIGNINGS_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None

        if channel is not None and hasattr(channel, "send"):
            manager = guild.get_member(offer["manager_id"])
            roster_count = sum(1 for guild_member in role.members if not guild_member.bot)
            embed = discord.Embed(
                title="Offer Accepted",
                description=f"{member.mention} has officially signed for {role.mention}!",
                color=discord.Color.green(),
            )
            embed.add_field(name="Player", value=member.mention, inline=True)
            embed.add_field(name="Team", value=role.mention, inline=True)
            embed.add_field(
                name="Manager / Franchise Owner",
                value=manager.mention if manager else f"<@{offer['manager_id']}>",
                inline=False,
            )
            embed.add_field(name="Current Roster", value=str(roster_count), inline=True)
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text="Team Management • Signing confirmed")
            await channel.send(embed=embed)
        else:
            print("Offer accepted, but SIGNINGS_CHANNEL_ID is missing or inaccessible.")

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id="team_offer:deny",
    )
    async def deny(self, interaction, button):
        offer = await self.check_offer(interaction)
        if not offer:
            return
        finish_offer(interaction.message.id, "denied")
        await interaction.response.defer()
        await self.close_buttons(interaction)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.tree.command(name="addteam", description="Add a team and assign its manager")
@app_commands.describe(manager="The team's manager", team_role="The team's Discord role")
async def addteam(interaction: discord.Interaction, manager: discord.Member, team_role: discord.Role):
    if not is_bot_owner(interaction.user.id):
        await reply(interaction, "Only a configured bot owner can add teams.")
        return

    manager_role = interaction.guild.get_role(MANAGER_ROLE_ID)
    if manager_role is None:
        await reply(interaction, "MANAGER_ROLE_ID is not configured correctly.")
        return

    previous_team = get_team_for_manager(manager.id)
    try:
        if previous_team and previous_team["team_role_id"] != team_role.id:
            previous_role = interaction.guild.get_role(previous_team["team_role_id"])
            if previous_role in manager.roles:
                await manager.remove_roles(previous_role, reason="Manager's assigned team changed")
        await manager.add_roles(manager_role, team_role, reason="Added as a team manager")
    except discord.Forbidden:
        await reply(interaction, "I cannot assign those roles. Put my bot role above them.")
        return

    with connect_db() as db:
        # A manager controls exactly one team. If /addteam is used again, replace
        # their old team mapping so /offer and /release cannot affect another role.
        db.execute("DELETE FROM teams WHERE manager_id = ?", (manager.id,))
        db.execute(
            "INSERT OR REPLACE INTO teams (team_role_id, manager_id) VALUES (?, ?)",
            (team_role.id, manager.id),
        )
    await reply(interaction, f"Added {team_role.mention} with {manager.mention} as manager.")


@bot.tree.command(name="offer", description="Send a player an offer to join your team")
@app_commands.describe(player="The player you want to offer")
async def offer(interaction: discord.Interaction, player: discord.Member):
    team = get_team_for_manager(interaction.user.id)
    if team is None:
        await reply(interaction, "You are not registered as a team manager.")
        return

    role = interaction.guild.get_role(team["team_role_id"])
    if role is None:
        await reply(interaction, "Your saved team role no longer exists.")
        return
    if role in player.roles:
        await reply(interaction, f"{player.mention} is already on {role.mention}.")
        return

    embed = discord.Embed(
        title="Team Offer",
        description=f"You have received an offer to join **{role.name}**.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Team", value=role.mention, inline=True)
    embed.add_field(name="Manager", value=interaction.user.mention, inline=True)
    embed.set_footer(text="Choose Accept or Deny below")

    view = OfferView()
    try:
        dm_message = await player.send(embed=embed, view=view)
    except discord.Forbidden:
        await reply(interaction, "I could not DM that player. They may have DMs disabled.")
        return

    view.message_id = dm_message.id
    with connect_db() as db:
        db.execute(
            "INSERT INTO offers (message_id, player_id, team_role_id, manager_id) VALUES (?, ?, ?, ?)",
            (dm_message.id, player.id, role.id, interaction.user.id),
        )
    bot.add_view(view, message_id=dm_message.id)
    await reply(interaction, f"Offer sent to {player.mention}.")


@bot.tree.command(name="release", description="Remove a player from your team")
@app_commands.describe(player="The player you want to release")
async def release(interaction: discord.Interaction, player: discord.Member):
    team = get_team_for_manager(interaction.user.id)
    if team is None:
        await reply(interaction, "You are not registered as a team manager.")
        return

    role = interaction.guild.get_role(team["team_role_id"])
    if role is None or role not in player.roles:
        await reply(interaction, "That player is not on your team.")
        return

    try:
        await player.remove_roles(role, reason=f"Released by {interaction.user}")
    except discord.Forbidden:
        await reply(interaction, "I cannot remove that role. Put my bot role above it.")
        return
    await reply(interaction, f"Released {player.mention} from {role.mention}.")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add it to Railway Variables or your .env file.")
    bot.run(TOKEN)
