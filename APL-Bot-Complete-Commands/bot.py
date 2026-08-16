import asyncio
import json
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
HEAD_COACH_ROLE_ID = int(os.getenv("HEAD_COACH_ROLE_ID", "0"))
ASSISTANT_COACH_ROLE_ID = int(os.getenv("ASSISTANT_COACH_ROLE_ID", "0"))
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
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                emoji TEXT,
                style TEXT NOT NULL DEFAULT 'primary',
                questions TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_panel_messages (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
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


def get_team_for_member(member):
    managed = get_team_for_manager(member.id)
    if managed:
        return managed
    member_role_ids = {role.id for role in member.roles}
    with connect_db() as db:
        teams = db.execute("SELECT * FROM teams").fetchall()
    return next((team for team in teams if team["team_role_id"] in member_role_ids), None)


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
        with connect_db() as db:
            panel_messages = db.execute("SELECT message_id FROM ticket_panel_messages").fetchall()
        for panel in panel_messages:
            ticket_types = get_ticket_types()
            if ticket_types:
                self.add_view(ConfiguredTicketPanelView(ticket_types), message_id=panel["message_id"])
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
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        print(f"Synced {len(synced)} slash commands")


bot = TeamBot(command_prefix="!", intents=intents)


async def reply(interaction, message, *, ephemeral=True):
    send_args = {"ephemeral": ephemeral}
    if isinstance(message, discord.Embed):
        send_args["embed"] = message
    else:
        send_args["content"] = message
    if interaction.response.is_done():
        await interaction.followup.send(**send_args)
    else:
        await interaction.response.send_message(**send_args)


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


def get_ticket_types():
    with connect_db() as db:
        return db.execute("SELECT * FROM ticket_types ORDER BY id").fetchall()


async def open_ticket_channel(interaction, ticket_type, answers=None):
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
    safe_user = re.sub(r"[^a-z0-9-]", "", interaction.user.display_name.lower().replace(" ", "-"))[:25] or "member"
    safe_type = re.sub(r"[^a-z0-9-]", "", ticket_type["label"].lower().replace(" ", "-"))[:20] or "support"
    channel = await guild.create_text_channel(
        f"{safe_type}-{safe_user}",
        category=category if isinstance(category, discord.CategoryChannel) else None,
        overwrites=overwrites,
        topic=f"apl-ticket-owner:{interaction.user.id}",
        reason=f"{ticket_type['label']} ticket opened",
    )
    staff_mentions = staff_role.mention if staff_role else "Server administrators"
    embed = discord.Embed(
        title="Ticket Opened",
        description=f"{interaction.user.mention} has created a new **{ticket_type['label']}** ticket.",
        color=discord.Color.purple(),
    )
    for question, answer in answers or []:
        embed.add_field(name=question, value=answer or "No answer provided", inline=False)
    embed.set_footer(text="APL Ticket System • Use the buttons below")
    await channel.send(content=f"{interaction.user.mention} {staff_mentions}", embed=embed, view=TicketControlsView())
    await reply(interaction, f"Your ticket is ready: {channel.mention}")


class TicketQuestionModal(discord.ui.Modal):
    def __init__(self, ticket_type):
        super().__init__(title=ticket_type["label"][:45])
        self.ticket_type = ticket_type
        self.question_inputs = []
        for question in json.loads(ticket_type["questions"])[:5]:
            field = discord.ui.TextInput(label=question[:45], style=discord.TextStyle.paragraph, max_length=500, required=True)
            self.question_inputs.append((question, field))
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        answers = [(question, field.value) for question, field in self.question_inputs]
        await open_ticket_channel(interaction, self.ticket_type, answers)


class TicketTypeButton(discord.ui.Button):
    def __init__(self, ticket_type):
        styles = {
            "primary": discord.ButtonStyle.primary,
            "blue": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "grey": discord.ButtonStyle.secondary,
            "gray": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "green": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger,
            "red": discord.ButtonStyle.danger,
        }
        self.ticket_type = ticket_type
        super().__init__(
            label=ticket_type["label"][:80],
            emoji=ticket_type["emoji"] or None,
            style=styles.get(ticket_type["style"].lower(), discord.ButtonStyle.primary),
            custom_id=f"apl_ticket:type:{ticket_type['id']}",
        )

    async def callback(self, interaction):
        questions = json.loads(self.ticket_type["questions"])
        if questions:
            await interaction.response.send_modal(TicketQuestionModal(self.ticket_type))
        else:
            await interaction.response.defer(ephemeral=True)
            await open_ticket_channel(interaction, self.ticket_type)


class ConfiguredTicketPanelView(discord.ui.View):
    def __init__(self, ticket_types):
        super().__init__(timeout=None)
        for index, ticket_type in enumerate(ticket_types[:25]):
            button = TicketTypeButton(ticket_type)
            button.row = index // 5
            self.add_item(button)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", emoji="🎫", style=discord.ButtonStyle.success, custom_id="apl_ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, button):
        await interaction.response.defer(ephemeral=True)
        await open_ticket_channel(interaction, {"label": "General Support"})


class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim Ticket", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="apl_ticket:claim")
    async def claim(self, interaction: discord.Interaction, button):
        if not is_admin(interaction) and not (TICKET_STAFF_ROLE_ID and interaction.user.get_role(TICKET_STAFF_ROLE_ID)):
            await reply(interaction, "Only ticket staff can claim tickets.")
            return
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="apl_ticket:close")
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


@bot.tree.command(name="commands", description="Show the commands available to players and managers")
async def command_list(interaction: discord.Interaction):
    embed = discord.Embed(title="APL Bot Commands", color=discord.Color.blurple())
    embed.add_field(
        name="PLAYER COMMANDS",
        value=(
            "`/myoffers` — View your pending offers\n"
            "`/teaminfo` — View your team information\n"
            "`/teams` — List all registered teams\n"
            "`/roster` — View your team's roster\n"
            "`/commands` — Show this command list"
        ),
        inline=False,
    )
    if get_team_for_manager(interaction.user.id):
        embed.add_field(
            name="MANAGER COMMANDS",
            value=(
                "`/offer` — Send a signing offer\n"
                "`/canceloffer` — Cancel a pending offer\n"
                "`/teamoffers` — View your team's pending offers\n"
                "`/release` — Release a player from your team\n"
                "`/promote` — Promote a player to coach\n"
                "`/demote` — Demote a coach back to player"
            ),
            inline=False,
        )
    if is_bot_owner(interaction.user.id):
        embed.add_field(
            name="OWNER / ADMIN COMMANDS",
            value=(
                "`/addteam` · `/ownerlist` · `/ticketpanel`\n"
                "`/addticketbutton` · `/deleteticketbutton` · `/totw`"
            ),
            inline=False,
        )
    embed.set_footer(text="Commands shown are based on your current role")
    await reply(interaction, embed, ephemeral=True)


@bot.tree.command(name="myoffers", description="View your pending team offers")
async def myoffers(interaction: discord.Interaction):
    with connect_db() as db:
        offers = db.execute(
            "SELECT * FROM offers WHERE player_id = ? AND status = 'pending' ORDER BY message_id DESC",
            (interaction.user.id,),
        ).fetchall()
    if not offers:
        await reply(interaction, "You do not have any pending offers.")
        return
    lines = []
    for pending in offers[:20]:
        role = interaction.guild.get_role(pending["team_role_id"])
        lines.append(f"• **{role.name if role else 'Deleted team'}** — Manager <@{pending['manager_id']}>")
    embed = discord.Embed(title="Your Pending Offers", description="\n".join(lines), color=discord.Color.blurple())
    await reply(interaction, embed)


@bot.tree.command(name="teaminfo", description="View information about your team")
async def teaminfo(interaction: discord.Interaction):
    team = get_team_for_member(interaction.user)
    if not team:
        await reply(interaction, "You are not currently registered with a team.")
        return
    role = interaction.guild.get_role(team["team_role_id"])
    if not role:
        await reply(interaction, "Your saved team role no longer exists.")
        return
    manager = interaction.guild.get_member(team["manager_id"])
    members = [member for member in role.members if not member.bot]
    embed = discord.Embed(title=role.name, description="APL Team Information", color=role.color if role.color.value else discord.Color.blurple())
    embed.add_field(name="Manager", value=manager.mention if manager else f"<@{team['manager_id']}>")
    embed.add_field(name="Roster", value=f"{len(members)}/{ROSTER_CAP}")
    embed.add_field(name="Team Role", value=role.mention, inline=False)
    if isinstance(role.display_icon, discord.Asset):
        embed.set_thumbnail(url=role.display_icon.url)
    await reply(interaction, embed)


@bot.tree.command(name="teams", description="List all registered teams")
async def teams(interaction: discord.Interaction):
    with connect_db() as db:
        saved_teams = db.execute("SELECT * FROM teams ORDER BY team_role_id").fetchall()
    lines = []
    for team in saved_teams:
        role = interaction.guild.get_role(team["team_role_id"])
        if role:
            lines.append(f"• {role.mention} — {len([m for m in role.members if not m.bot])}/{ROSTER_CAP} — Manager <@{team['manager_id']}>")
    embed = discord.Embed(title="APL Teams", description="\n".join(lines) if lines else "No teams have been added yet.", color=discord.Color.blurple())
    await reply(interaction, embed)


@bot.tree.command(name="roster", description="View your team's current roster")
async def roster(interaction: discord.Interaction):
    team = get_team_for_member(interaction.user)
    if not team:
        await reply(interaction, "You are not currently registered with a team.")
        return
    role = interaction.guild.get_role(team["team_role_id"])
    if not role:
        await reply(interaction, "Your saved team role no longer exists.")
        return
    members = [member for member in role.members if not member.bot]
    roster_text = "\n".join(f"• {member.mention}" for member in members) or "No players"
    embed = discord.Embed(title=f"{role.name} Roster", description=roster_text, color=role.color if role.color.value else discord.Color.blurple())
    embed.set_footer(text=f"{len(members)}/{ROSTER_CAP} roster places used")
    await reply(interaction, embed)


@bot.tree.command(name="ownerlist", description="View the bot's owner configuration")
@app_commands.default_permissions(administrator=True)
async def ownerlist(interaction: discord.Interaction):
    if not is_bot_owner(interaction.user.id):
        await reply(interaction, "Only a configured bot owner can view this.")
        return
    owners = "\n".join(f"• <@{owner_id}> (`{owner_id}`)" for owner_id in OWNER_IDS)
    embed = discord.Embed(title="APL Bot Owners", description=owners or "No owner IDs configured.", color=discord.Color.gold())
    embed.add_field(name="Server", value=f"`{GUILD_ID}`", inline=False)
    await reply(interaction, embed)


@bot.tree.command(name="canceloffer", description="Cancel a pending offer sent to a player")
@app_commands.describe(player="Player whose offer you want to cancel")
async def canceloffer(interaction: discord.Interaction, player: discord.Member):
    team = get_team_for_manager(interaction.user.id)
    if not team:
        await reply(interaction, "You are not registered as a team manager.")
        return
    with connect_db() as db:
        pending = db.execute(
            "SELECT message_id FROM offers WHERE manager_id = ? AND player_id = ? AND team_role_id = ? AND status = 'pending' ORDER BY message_id DESC LIMIT 1",
            (interaction.user.id, player.id, team["team_role_id"]),
        ).fetchone()
        if pending:
            db.execute("UPDATE offers SET status = 'cancelled' WHERE message_id = ?", (pending["message_id"],))
    await reply(interaction, f"Cancelled the pending offer for {player.mention}." if pending else f"There is no pending offer for {player.mention}.")


@bot.tree.command(name="teamoffers", description="View all pending offers for your team")
async def teamoffers(interaction: discord.Interaction):
    team = get_team_for_manager(interaction.user.id)
    if not team:
        await reply(interaction, "You are not registered as a team manager.")
        return
    with connect_db() as db:
        offers = db.execute(
            "SELECT * FROM offers WHERE manager_id = ? AND team_role_id = ? AND status = 'pending' ORDER BY message_id DESC",
            (interaction.user.id, team["team_role_id"]),
        ).fetchall()
    role = interaction.guild.get_role(team["team_role_id"])
    text = "\n".join(f"• <@{pending['player_id']}>" for pending in offers[:20]) or "No pending offers."
    embed = discord.Embed(title=f"{role.name if role else 'Team'} Pending Offers", description=text, color=discord.Color.blurple())
    await reply(interaction, embed)


@bot.tree.command(name="promote", description="Promote one of your players to a coaching role")
@app_commands.describe(player="Player on your team", position="Coaching role to assign")
@app_commands.choices(position=[
    app_commands.Choice(name="Head Coach", value="head"),
    app_commands.Choice(name="Assistant Coach", value="assistant"),
])
async def promote(interaction: discord.Interaction, player: discord.Member, position: app_commands.Choice[str]):
    team = get_team_for_manager(interaction.user.id)
    if not team:
        await reply(interaction, "You are not registered as a team manager.")
        return
    team_role = interaction.guild.get_role(team["team_role_id"])
    if not team_role or team_role not in player.roles:
        await reply(interaction, "That player is not on your team.")
        return
    role_id = HEAD_COACH_ROLE_ID if position.value == "head" else ASSISTANT_COACH_ROLE_ID
    coach_role = interaction.guild.get_role(role_id)
    if not coach_role:
        await reply(interaction, "That coach role is not configured correctly in Railway.")
        return
    other_role_id = ASSISTANT_COACH_ROLE_ID if position.value == "head" else HEAD_COACH_ROLE_ID
    other_role = interaction.guild.get_role(other_role_id)
    try:
        if other_role and other_role in player.roles:
            await player.remove_roles(other_role, reason="Coach position changed")
        await player.add_roles(coach_role, reason=f"Promoted by {interaction.user}")
    except discord.Forbidden:
        await reply(interaction, "I cannot manage that coach role. Put my bot role above it.")
        return
    await reply(interaction, f"Promoted {player.mention} to {coach_role.mention}.")


@bot.tree.command(name="demote", description="Demote a coach back to player")
@app_commands.describe(player="Coach on your team")
async def demote(interaction: discord.Interaction, player: discord.Member):
    team = get_team_for_manager(interaction.user.id)
    if not team:
        await reply(interaction, "You are not registered as a team manager.")
        return
    team_role = interaction.guild.get_role(team["team_role_id"])
    if not team_role or team_role not in player.roles:
        await reply(interaction, "That player is not on your team.")
        return
    coach_roles = [role for role_id in (HEAD_COACH_ROLE_ID, ASSISTANT_COACH_ROLE_ID) if (role := interaction.guild.get_role(role_id)) and role in player.roles]
    if not coach_roles:
        await reply(interaction, "That player does not have a configured coach role.")
        return
    try:
        await player.remove_roles(*coach_roles, reason=f"Demoted by {interaction.user}")
    except discord.Forbidden:
        await reply(interaction, "I cannot remove that coach role. Put my bot role above it.")
        return
    await reply(interaction, f"Demoted {player.mention} back to player.")


@bot.tree.command(name="ticketpanel", description="Post the APL support ticket panel")
@app_commands.default_permissions(administrator=True)
async def ticketpanel(interaction: discord.Interaction):
    if not is_admin(interaction):
        await reply(interaction, "Only server administrators can create ticket panels.")
        return
    ticket_types = get_ticket_types()
    embed = discord.Embed(
        title="Help & Support",
        description="Click one of the buttons below to create a private support ticket.",
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Powered by APL Ticket System")
    view = ConfiguredTicketPanelView(ticket_types) if ticket_types else TicketPanelView()
    panel_message = await interaction.channel.send(embed=embed, view=view)
    with connect_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO ticket_panel_messages (message_id, channel_id) VALUES (?, ?)",
            (panel_message.id, interaction.channel.id),
        )
    await reply(interaction, "Ticket panel posted.")


@bot.tree.command(name="addticketbutton", description="Add or update a button on the ticket panel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    name="Button and ticket name, for example Manager Applications",
    color="blue, grey, green, or red",
    emoji="Optional emoji displayed on the button",
    questions="Up to 5 questions separated with |",
)
async def addticketbutton(
    interaction: discord.Interaction,
    name: str,
    color: str = "blue",
    emoji: str = "🎫",
    questions: str = "",
):
    if not is_admin(interaction):
        await reply(interaction, "Only server administrators can configure ticket buttons.")
        return
    valid_colors = {"blue", "primary", "grey", "gray", "secondary", "green", "success", "red", "danger"}
    if color.lower() not in valid_colors:
        await reply(interaction, "Color must be blue, grey, green, or red.")
        return
    question_list = [question.strip() for question in questions.split("|") if question.strip()]
    if len(question_list) > 5:
        await reply(interaction, "Discord forms allow a maximum of 5 questions per ticket button.")
        return
    with connect_db() as db:
        current_count = db.execute("SELECT COUNT(*) FROM ticket_types").fetchone()[0]
        existing = db.execute("SELECT id FROM ticket_types WHERE lower(label) = lower(?)", (name,)).fetchone()
        if current_count >= 25 and not existing:
            await reply(interaction, "A ticket panel can contain a maximum of 25 buttons.")
            return
        if existing:
            db.execute(
                "UPDATE ticket_types SET label = ?, emoji = ?, style = ?, questions = ? WHERE id = ?",
                (name[:80], emoji, color.lower(), json.dumps(question_list), existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO ticket_types (label, emoji, style, questions) VALUES (?, ?, ?, ?)",
                (name[:80], emoji, color.lower(), json.dumps(question_list)),
            )
    await reply(interaction, f"Saved the **{name}** ticket button. Run `/ticketpanel` to post the updated panel.")


@bot.tree.command(name="deleteticketbutton", description="Remove a saved ticket button")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(name="Exact ticket button name to remove")
async def deleteticketbutton(interaction: discord.Interaction, name: str):
    if not is_admin(interaction):
        await reply(interaction, "Only server administrators can configure ticket buttons.")
        return
    with connect_db() as db:
        cursor = db.execute("DELETE FROM ticket_types WHERE lower(label) = lower(?)", (name,))
    if cursor.rowcount:
        await reply(interaction, f"Removed the **{name}** ticket button. Post a new `/ticketpanel` to show the change.")
    else:
        await reply(interaction, f"I could not find a ticket button named **{name}**.")


@bot.tree.command(name="totw", description="Start a private Team of the Week screenshot upload")
@app_commands.default_permissions(administrator=True)
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
