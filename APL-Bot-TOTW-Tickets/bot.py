import asyncio
import os
import re
import sqlite3
from io import BytesIO
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MANAGER_ROLE_ID = int(os.getenv("MANAGER_ROLE_ID", "0"))
SIGNINGS_CHANNEL_ID = int(os.getenv("SIGNINGS_CHANNEL_ID", "0"))
ROSTER_CAP = int(os.getenv("ROSTER_CAP", "22"))
LEAGUE_NAME = os.getenv("LEAGUE_NAME", "APL | RELOAD | FC26")
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
TICKET_STAFF_ROLE_ID = int(os.getenv("TICKET_STAFF_ROLE_ID", "0"))
TOTW_CATEGORY_ID = int(os.getenv("TOTW_CATEGORY_ID", "0"))
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
        self.add_view(TicketPanelView())
        self.add_view(TicketControlsView())
        self.add_view(TotwControlsView())
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
            description=f"You accepted the offer for **{role.name}**!",
            color=discord.Color.green(),
        )
        # Server role mentions show as @unknown-role inside DMs, so use its name.
        accepted_embed.add_field(name="Team", value=role.name, inline=True)
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
            manager_text = manager.mention if manager else f"<@{offer['manager_id']}>"
            roster_count = sum(1 for guild_member in role.members if not guild_member.bot)
            embed = discord.Embed(
                title="Offer Accepted",
                description=f"{member.mention} **{member.display_name}** has signed with {role.mention}",
                color=role.color if role.color.value else discord.Color.blurple(),
            )
            embed.set_author(
                name=LEAGUE_NAME,
                icon_url=guild.icon.url if guild.icon else None,
            )
            embed.add_field(
                name="⚽ Franchise Owner",
                value=f"{role.mention} • {manager_text}",
                inline=False,
            )
            embed.add_field(
                name="📋 Roster Cap",
                value=f"{roster_count}/{ROSTER_CAP}",
                inline=False,
            )

            # Prefer the team's role icon, then fall back to the server icon.
            role_icon = role.display_icon
            if isinstance(role_icon, discord.Asset):
                embed.set_thumbnail(url=role_icon.url)
            elif guild.icon:
                embed.set_thumbnail(url=guild.icon.url)

            embed.set_footer(text="APL BOT • Signing confirmed")
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


def is_admin(interaction):
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", emoji="🎫", style=discord.ButtonStyle.success, custom_id="apl_ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, button):
        guild = interaction.guild
        existing = discord.utils.find(
            lambda channel: channel.topic == f"apl-ticket-owner:{interaction.user.id}",
            guild.text_channels,
        )
        if existing:
            await reply(interaction, f"You already have an open ticket: {existing.mention}")
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        staff_role = guild.get_role(TICKET_STAFF_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        category = guild.get_channel(TICKET_CATEGORY_ID)
        safe_name = re.sub(r"[^a-z0-9-]", "", interaction.user.display_name.lower().replace(" ", "-"))[:40] or "member"
        channel = await guild.create_text_channel(
            f"ticket-{safe_name}",
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            topic=f"apl-ticket-owner:{interaction.user.id}",
            reason="APL ticket opened",
        )
        embed = discord.Embed(title="Support Ticket", description=f"Welcome {interaction.user.mention}. Describe what you need help with and a staff member will respond.", color=discord.Color.blurple())
        embed.set_footer(text="Use the controls below to claim or close this ticket")
        await channel.send(content=interaction.user.mention, embed=embed, view=TicketControlsView())
        await reply(interaction, f"Your ticket is ready: {channel.mention}")


class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.primary, custom_id="apl_ticket:claim")
    async def claim(self, interaction: discord.Interaction, button):
        if not is_admin(interaction) and not (TICKET_STAFF_ROLE_ID and interaction.user.get_role(TICKET_STAFF_ROLE_ID)):
            await reply(interaction, "Only ticket staff can claim tickets.")
            return
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="apl_ticket:close")
    async def close(self, interaction: discord.Interaction, button):
        owner_id = int(interaction.channel.topic.split(":")[-1]) if interaction.channel.topic and interaction.channel.topic.startswith("apl-ticket-owner:") else 0
        if interaction.user.id != owner_id and not is_admin(interaction) and not (TICKET_STAFF_ROLE_ID and interaction.user.get_role(TICKET_STAFF_ROLE_ID)):
            await reply(interaction, "Only the ticket owner or staff can close this ticket.")
            return
        await interaction.response.send_message("Ticket closed. This channel will be deleted in 5 seconds.")
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


POSITIONS = {"GK", "LB", "LWB", "LCB", "CB", "RCB", "RB", "RWB", "LDM", "CDM", "RDM", "LCM", "CM", "RCM", "CAM", "LM", "RM", "LW", "LS", "ST", "RS", "RW", "CF"}
DEFENDERS = {"LB", "LWB", "LCB", "CB", "RCB", "RB", "RWB"}
MIDFIELDERS = {"LDM", "CDM", "RDM", "LCM", "CM", "RCM", "CAM", "LM", "RM"}
FORWARDS = {"LW", "LS", "ST", "RS", "RW", "CF"}


def read_player_rows(image_path, source_name):
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result, _ = engine(str(image_path))
    if not result:
        return []

    tokens = []
    for box, text, confidence in result:
        if confidence < 0.35:
            continue
        center_y = sum(point[1] for point in box) / 4
        left_x = min(point[0] for point in box)
        tokens.append((center_y, left_x, text.strip()))

    lines = []
    for center_y, left_x, text in sorted(tokens):
        row = next((item for item in lines if abs(item[0] - center_y) < 18), None)
        if row is None:
            row = [center_y, []]
            lines.append(row)
        row[1].append((left_x, text))

    players = []
    row_pattern = re.compile(r"^(GK|LB|LWB|LCB|CB|RCB|RB|RWB|LDM|CDM|RDM|LCM|CM|RCM|CAM|LM|RM|LW|LS|ST|RS|RW|CF)\s+(.+?)\s+(10[.,]0|[0-9][.,][0-9])\s+(\d+)\s+(\d+)$", re.I)
    for _, pieces in lines:
        line = " ".join(text for _, text in sorted(pieces)).replace("|", " ")
        line = re.sub(r"\s+", " ", line).strip()
        match = row_pattern.match(line)
        if match:
            players.append({
                "position": match.group(1).upper(),
                "name": match.group(2).strip(),
                "rating": float(match.group(3).replace(",", ".")),
                "goals": int(match.group(4)),
                "assists": int(match.group(5)),
                "source": source_name,
            })
    return players


def choose_totw(players):
    def ranked(items):
        return sorted(items, key=lambda player: player["rating"] + player["goals"] * 0.35 + player["assists"] * 0.25, reverse=True)

    chosen = []
    chosen += ranked([p for p in players if p["position"] == "GK"])[:1]
    chosen += ranked([p for p in players if p["position"] in DEFENDERS])[:4]
    chosen += ranked([p for p in players if p["position"] in MIDFIELDERS])[:3]
    chosen += ranked([p for p in players if p["position"] in FORWARDS])[:3]
    chosen_ids = {id(player) for player in chosen}
    chosen += ranked([p for p in players if id(p) not in chosen_ids])[: 11 - len(chosen)]
    return chosen[:11]


def create_totw_image(players):
    image = Image.new("RGB", (1600, 900), "#080b09")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 50)
        name_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except OSError:
        title_font = name_font = small_font = ImageFont.load_default()

    draw.text((55, 35), "APL TEAM OF THE WEEK", fill="#ccff3d", font=title_font)
    draw.rounded_rectangle((260, 100, 1340, 820), radius=25, fill="#14200f", outline="#8fbd2c", width=4)
    draw.line((800, 100, 800, 820), fill="#6e8e36", width=3)
    draw.ellipse((690, 340, 910, 560), outline="#6e8e36", width=3)
    points = [(800, 740), (450, 620), (670, 660), (930, 660), (1150, 620), (520, 430), (800, 510), (1080, 430), (470, 230), (800, 270), (1130, 230)]
    for player, (x, y) in zip(players, points):
        draw.rounded_rectangle((x - 120, y - 55, x + 120, y + 55), radius=14, fill="#0c110c", outline="#ccff3d", width=3)
        draw.text((x, y - 42), f'{player["position"]}  {player["rating"]:.1f}', fill="#ccff3d", font=small_font, anchor="ma")
        draw.text((x, y - 8), player["name"][:18], fill="white", font=name_font, anchor="ma")
        draw.text((x, y + 28), f'{player["goals"]} G  •  {player["assists"]} A', fill="#aeb6ae", font=small_font, anchor="ma")
    output = BytesIO()
    image.save(output, "PNG")
    output.seek(0)
    return output


class TotwControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Calculate TOTW", emoji="⭐", style=discord.ButtonStyle.success, custom_id="apl_totw:calculate")
    async def calculate(self, interaction: discord.Interaction, button):
        if not is_admin(interaction):
            await reply(interaction, "Only server administrators can calculate TOTW.")
            return
        await interaction.response.defer()
        attachments = []
        async for message in interaction.channel.history(limit=None, oldest_first=True):
            attachments.extend(item for item in message.attachments if item.content_type and item.content_type.startswith("image/"))
        if not attachments:
            await interaction.followup.send("Upload Player Performance screenshots in this channel first.", ephemeral=True)
            return

        folder = DATABASE_PATH.parent / "totw" / str(interaction.channel.id)
        folder.mkdir(parents=True, exist_ok=True)
        all_players = []
        status = await interaction.followup.send(f"Reading {len(attachments)} screenshots…", wait=True)
        for index, attachment in enumerate(attachments, start=1):
            path = folder / f"{index}-{re.sub(r'[^a-zA-Z0-9._-]', '_', attachment.filename)}"
            await attachment.save(path)
            players = await asyncio.to_thread(read_player_rows, path, attachment.filename)
            all_players.extend(players)
            await status.edit(content=f"Reading screenshots… {index}/{len(attachments)}")

        selected = choose_totw(all_players)
        if len(selected) < 11:
            await status.edit(content=f"I found only {len(all_players)} readable player rows and need at least 11. Upload clearer screenshots showing POS, Name, RR, G and AST, then try again.")
            return
        image = await asyncio.to_thread(create_totw_image, selected)
        list_text = "\n".join(f'**{i}. {p["position"]} — {p["name"]}** · {p["rating"]:.1f} RR · {p["goals"]}G {p["assists"]}A' for i, p in enumerate(selected, 1))
        embed = discord.Embed(title="APL Team of the Week", description=list_text, color=discord.Color.green())
        embed.set_footer(text=f"Calculated from {len(attachments)} screenshots and {len(all_players)} players")
        await status.edit(content="TOTW calculation complete.")
        await interaction.channel.send(embed=embed, file=discord.File(image, filename="APL-TOTW.png"))

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger, custom_id="apl_totw:cancel")
    async def cancel(self, interaction: discord.Interaction, button):
        if not is_admin(interaction):
            await reply(interaction, "Only server administrators can close this session.")
            return
        await interaction.response.send_message("Closing this TOTW upload channel in 5 seconds.")
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"TOTW session closed by {interaction.user}")


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
    # Server role mentions cannot be resolved inside DMs and display as
    # @unknown-role. Showing the role name keeps the offer readable.
    embed.add_field(name="Team", value=role.name, inline=True)
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


@bot.tree.command(name="ticketpanel", description="Post the APL support ticket panel")
async def ticketpanel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await reply(interaction, "Only server administrators can create ticket panels.")
        return
    embed = discord.Embed(
        title="APL Support",
        description="Need help? Press **Create Ticket** to open a private support channel with the staff team.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="APL Ticket System")
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await reply(interaction, "Ticket panel posted.")


@bot.tree.command(name="totw", description="Start a private Team of the Week screenshot upload")
async def totw(interaction: discord.Interaction):
    if not is_admin(interaction):
        await reply(interaction, "Only server administrators can use TOTW.")
        return
    guild = interaction.guild
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True),
    }
    category = guild.get_channel(TOTW_CATEGORY_ID)
    channel = await guild.create_text_channel(
        f"totw-{interaction.user.display_name}"[:90],
        category=category if isinstance(category, discord.CategoryChannel) else None,
        overwrites=overwrites,
        topic=f"apl-totw-owner:{interaction.user.id}",
        reason="APL TOTW upload session",
    )
    embed = discord.Embed(
        title="TOTW Screenshot Upload",
        description=(
            "Upload as many **Player Performance** screenshots as you need in this channel.\n\n"
            "Each screenshot must show the table columns **POS, Name, RR, G and AST**. "
            "When all screenshots are uploaded, press **Calculate TOTW**."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(name="Selection", value="1 GK • 4 defenders • 3 midfielders • 3 forwards", inline=False)
    embed.add_field(name="Ranking", value="Match rating first, with bonuses for goals and assists", inline=False)
    await channel.send(content=interaction.user.mention, embed=embed, view=TotwControlsView())
    await reply(interaction, f"Your private TOTW upload channel is ready: {channel.mention}")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add it to Railway Variables or your .env file.")
    bot.run(TOKEN)
