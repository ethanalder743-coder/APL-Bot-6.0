import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from io import BytesIO
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from aiohttp import web


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
WEB_PORT = int(os.getenv("PORT", "8080"))
WEBSITE_DIR = Path(__file__).parent / "website"
WEB_OWNER_EMAIL = os.getenv("WEB_OWNER_EMAIL", "").strip().lower()
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "").strip().rstrip("/")
if WEB_BASE_URL and not WEB_BASE_URL.startswith(("https://", "http://")):
    WEB_BASE_URL = f"https://{WEB_BASE_URL}"
WEB_SESSION_SECONDS = 315360000  # Ten years; invited emails remain approved permanently.


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
        db.execute("CREATE TABLE IF NOT EXISTS bot_admins (user_id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS guild_admins (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id))")
        db.execute("CREATE TABLE IF NOT EXISTS bot_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS rolesaver_excluded (role_id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS saved_member_roles (user_id INTEGER PRIMARY KEY, role_ids TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS warnings (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("CREATE TABLE IF NOT EXISTS sticky_messages (channel_id INTEGER PRIMARY KEY, content TEXT NOT NULL, message_id INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS gametime_roles (role_id INTEGER PRIMARY KEY)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS signed_players (guild_id INTEGER NOT NULL, team_role_id INTEGER NOT NULL, player_id INTEGER NOT NULL, PRIMARY KEY (guild_id, player_id))"
        )
        db.execute("CREATE TABLE IF NOT EXISTS web_admins (email TEXT PRIMARY KEY, invited_by TEXT, created_at INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS web_login_tokens (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS web_sessions (session_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires_at INTEGER NOT NULL)")
        if WEB_OWNER_EMAIL:
            db.execute(
                "INSERT OR IGNORE INTO web_admins (email, invited_by, created_at) VALUES (?, 'website-owner', ?)",
                (WEB_OWNER_EMAIL, int(time.time())),
            )
        team_columns = {row["name"] for row in db.execute("PRAGMA table_info(teams)").fetchall()}
        if "guild_id" not in team_columns:
            db.execute("ALTER TABLE teams ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
            if GUILD_ID:
                db.execute("UPDATE teams SET guild_id = ? WHERE guild_id = 0", (GUILD_ID,))
        # Keep one team per manager in each server without deleting that same
        # manager's team in another server.
        db.execute(
            """
            DELETE FROM teams
            WHERE rowid NOT IN (
                SELECT MAX(rowid) FROM teams GROUP BY guild_id, manager_id
            )
            """
        )
        db.execute("DROP INDEX IF EXISTS one_team_per_manager")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_team_per_guild_manager ON teams(guild_id, manager_id)")
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
        offer_columns = {row["name"] for row in db.execute("PRAGMA table_info(offers)").fetchall()}
        if "guild_id" not in offer_columns:
            db.execute("ALTER TABLE offers ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
            if GUILD_ID:
                db.execute("UPDATE offers SET guild_id = ? WHERE guild_id = 0", (GUILD_ID,))
        roster_migrated = db.execute(
            "SELECT 1 FROM bot_settings WHERE setting_key = 'signed_players_migrated'"
        ).fetchone()
        if not roster_migrated:
            db.execute(
                """
                INSERT OR REPLACE INTO signed_players (guild_id, team_role_id, player_id)
                SELECT guild_id, team_role_id, player_id FROM offers
                WHERE status = 'accepted' AND guild_id != 0
                ORDER BY message_id ASC
                """
            )
            db.execute(
                "INSERT OR REPLACE INTO bot_settings (setting_key, setting_value) VALUES ('signed_players_migrated', '1')"
            )
        ticket_columns = {row["name"] for row in db.execute("PRAGMA table_info(ticket_types)").fetchall()}
        if "ping_role_ids" not in ticket_columns:
            db.execute("ALTER TABLE ticket_types ADD COLUMN ping_role_ids TEXT NOT NULL DEFAULT '[]'")


def get_team_for_manager(manager_id, guild_id):
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM teams WHERE manager_id = ? AND guild_id = ?", (manager_id, guild_id)
        ).fetchone()


def get_team_for_member(member):
    managed = get_team_for_manager(member.id, member.guild.id)
    if managed:
        return managed
    member_role_ids = {role.id for role in member.roles}
    with connect_db() as db:
        teams = db.execute("SELECT * FROM teams WHERE guild_id = ?", (member.guild.id,)).fetchall()
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


def get_setting(key, default=""):
    with connect_db() as db:
        row = db.execute("SELECT setting_value FROM bot_settings WHERE setting_key = ?", (key,)).fetchone()
    return row["setting_value"] if row else default


def set_setting(key, value):
    with connect_db() as db:
        db.execute("INSERT OR REPLACE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)", (key, str(value)))


def is_saved_admin(user_id, guild=None):
    if is_bot_owner(user_id):
        return True
    if guild and guild.owner_id == user_id:
        return True
    with connect_db() as db:
        if guild:
            return db.execute("SELECT 1 FROM guild_admins WHERE guild_id = ? AND user_id = ?", (guild.id, user_id)).fetchone() is not None
        return False


def member_is_admin(member):
    return bool(member and (member.guild_permissions.administrator or is_saved_admin(member.id, member.guild)))


def get_guild_setting(guild, key, default=""):
    return get_setting(f"guild:{guild.id}:{key}", default)


def set_guild_setting(guild, key, value):
    set_setting(f"guild:{guild.id}:{key}", value)


def get_roster_cap(guild):
    return int(get_guild_setting(guild, "roster_cap", str(ROSTER_CAP)) or ROSTER_CAP)


def get_team_players(role, manager_id):
    with connect_db() as db:
        rows = db.execute(
            "SELECT player_id FROM signed_players WHERE guild_id = ? AND team_role_id = ?",
            (role.guild.id, role.id),
        ).fetchall()
    players = []
    for row in rows:
        member = role.guild.get_member(row["player_id"])
        if member and not member.bot and member.id != manager_id and role in member.roles:
            players.append(member)
    return players


async def require_signings_channel(interaction):
    """Keep manager signing tools in the server's configured signings channel."""
    if is_admin(interaction):
        return True
    channel_id = int(get_guild_setting(interaction.guild, "signings_channel_id", str(SIGNINGS_CHANNEL_ID)) or 0)
    if not channel_id:
        await reply(interaction, "An admin must configure the signings channel with `/setsigningschannel` first.")
        return False
    if interaction.channel_id != channel_id:
        await reply(interaction, f"Manager commands can only be used in <#{channel_id}>.")
        return False
    return True


async def send_log(guild, title, description, color=discord.Color.blurple()):
    channel_id = int(get_guild_setting(guild, "log_channel_id", "0") or 0)
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    embed.set_footer(text="APL Bot Logs")
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class TeamBot(commands.Bot):
    async def setup_hook(self):
        setup_database()
        await start_website(self)
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

        global_synced = await self.tree.sync()
        removed_guild_duplicates = []
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            # Older builds copied every global command into the main guild,
            # which made commands such as /addadmin appear twice. Clear those
            # guild-only copies and keep one global command set for all servers.
            self.tree.clear_commands(guild=guild)
            removed_guild_duplicates = await self.tree.sync(guild=guild)
        print(f"Synced {len(global_synced)} global commands; cleared duplicate test-server commands")


bot = TeamBot(command_prefix="?", intents=intents)


async def website_home(request):
    return web.FileResponse(WEBSITE_DIR / "index.html")


async def website_dashboard(request):
    return web.FileResponse(WEBSITE_DIR / "dashboard.html")


async def website_asset(request):
    filename = request.match_info["filename"]
    allowed = {
        "apl-reload-logo.png",
        "elite-cup.png",
        "tnt-sports-cup.png",
        "manifest.webmanifest",
        "service-worker.js",
        "redesign.css",
        "premium.css",
        "dashboard.css",
        "dashboard-extra.css",
        "dashboard.js",
        "og.png",
    }
    if filename not in allowed:
        raise web.HTTPNotFound()
    return web.FileResponse(WEBSITE_DIR / filename)


async def website_data(request):
    raw_data = get_setting("website_data", "{}") or "{}"
    try:
        site_data = json.loads(raw_data)
    except json.JSONDecodeError:
        site_data = {}

    bot_teams = []
    if GUILD_ID:
        guild = bot.get_guild(GUILD_ID)
        if guild:
            with connect_db() as db:
                saved_teams = db.execute(
                    "SELECT team_role_id, manager_id FROM teams WHERE guild_id = ?", (guild.id,)
                ).fetchall()
            for saved_team in saved_teams:
                role = guild.get_role(saved_team["team_role_id"])
                if role:
                    manager = guild.get_member(saved_team["manager_id"])
                    bot_teams.append({
                        "id": str(role.id),
                        "name": role.name,
                        "managerName": manager.display_name if manager else "Manager TBA",
                        "rosterCount": len(get_team_players(role, saved_team["manager_id"])),
                        "rosterCap": get_roster_cap(guild),
                    })
    return web.json_response({"siteData": site_data, "botTeams": bot_teams})


def token_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def current_web_admin(request):
    session_token = request.cookies.get("apl_admin_session", "")
    if not session_token:
        return None
    now = int(time.time())
    with connect_db() as db:
        db.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))
        session = db.execute(
            "SELECT email FROM web_sessions WHERE session_hash = ? AND expires_at > ?",
            (token_hash(session_token), now),
        ).fetchone()
    return session["email"] if session else None


def create_website_login_url(email):
    if not WEB_BASE_URL:
        raise RuntimeError("WEB_BASE_URL is not configured")
    token = secrets.token_urlsafe(32)
    with connect_db() as db:
        db.execute(
            "INSERT INTO web_login_tokens (token_hash, email, expires_at) VALUES (?, ?, ?)",
            (token_hash(token), email, int(time.time()) + 1800),
        )
    return f"{WEB_BASE_URL}/auth/verify?token={token}"


async def verify_admin_email(request):
    token = request.query.get("token", "")
    now = int(time.time())
    with connect_db() as db:
        login = db.execute(
            "SELECT email FROM web_login_tokens WHERE token_hash = ? AND expires_at > ?",
            (token_hash(token), now),
        ).fetchone()
        if not login:
            raise web.HTTPUnauthorized(text="This sign-in link is invalid or has expired.")
        db.execute("DELETE FROM web_login_tokens WHERE token_hash = ?", (token_hash(token),))
        session_token = secrets.token_urlsafe(40)
        db.execute(
            "INSERT INTO web_sessions (session_hash, email, expires_at) VALUES (?, ?, ?)",
            (token_hash(session_token), login["email"], now + WEB_SESSION_SECONDS),
        )
    response = web.HTTPFound("/dashboard")
    response.set_cookie(
        "apl_admin_session",
        session_token,
        max_age=WEB_SESSION_SECONDS,
        httponly=True,
        secure=WEB_BASE_URL.startswith("https://"),
        samesite="Lax",
    )
    return response


async def website_admin_status(request):
    email = current_web_admin(request)
    if not email:
        raise web.HTTPUnauthorized(text="Sign in required")
    return web.json_response({"authorized": True, "email": email, "owner": email == WEB_OWNER_EMAIL})


async def website_admin_logout(request):
    session_token = request.cookies.get("apl_admin_session", "")
    if session_token:
        with connect_db() as db:
            db.execute("DELETE FROM web_sessions WHERE session_hash = ?", (token_hash(session_token),))
    response = web.json_response({"signedOut": True})
    response.del_cookie("apl_admin_session", path="/")
    return response


async def invite_website_admin(request):
    signed_in_email = current_web_admin(request)
    if not signed_in_email:
        raise web.HTTPForbidden(text="Sign in as a website administrator first")
    try:
        payload = await request.json()
        email = str(payload.get("email", "")).strip().lower()
    except (json.JSONDecodeError, web.HTTPBadRequest, AttributeError):
        raise web.HTTPBadRequest(text="Enter a valid email address")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise web.HTTPBadRequest(text="Enter a valid email address")
    with connect_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO web_admins (email, invited_by, created_at) VALUES (?, ?, ?)",
            (email, signed_in_email, int(time.time())),
        )
    try:
        invite_url = create_website_login_url(email)
    except RuntimeError as error:
        raise web.HTTPServiceUnavailable(text=str(error))
    return web.json_response({"invited": True, "email": email, "inviteUrl": invite_url})


async def save_website_data(request):
    if not current_web_admin(request):
        raise web.HTTPUnauthorized(text="Sign in required")
    try:
        payload = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest):
        raise web.HTTPBadRequest(text="Invalid website data")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Website data must be an object")
    set_setting("website_data", json.dumps(payload))
    return web.json_response({"saved": True})


BOT_DASHBOARD_SETTING_KEYS = {
    "signings_channel_id", "log_channel_id", "welcome_channel_id", "welcome_message",
    "roster_cap", "manager_role_id", "gametime_channel_id", "ticket_category_id",
    "ticket_staff_role_id", "totw_category_id", "rolesaver_enabled",
}


def dashboard_guild():
    return bot.get_guild(GUILD_ID) if GUILD_ID else None


async def website_bot_config(request):
    if not current_web_admin(request):
        raise web.HTTPUnauthorized(text="Sign in required")
    guild = dashboard_guild()
    if not guild:
        raise web.HTTPServiceUnavailable(text="The configured Discord server is not available")
    settings = {key: get_guild_setting(guild, key, "") for key in BOT_DASHBOARD_SETTING_KEYS}
    settings["roster_cap"] = settings["roster_cap"] or str(ROSTER_CAP)
    settings["rolesaver_enabled"] = settings["rolesaver_enabled"] or "false"
    with connect_db() as db:
        excluded_roles = [str(row["role_id"]) for row in db.execute("SELECT role_id FROM rolesaver_excluded").fetchall()]
        gametime_roles = [str(row["role_id"]) for row in db.execute("SELECT role_id FROM gametime_roles").fetchall()]
        ticket_rows = db.execute("SELECT * FROM ticket_types ORDER BY id").fetchall()
        team_rows = db.execute("SELECT * FROM teams WHERE guild_id = ?", (guild.id,)).fetchall()
    tickets = []
    for row in ticket_rows:
        tickets.append({
            "id": row["id"], "label": row["label"], "emoji": row["emoji"] or "🎫", "style": row["style"],
            "questions": json.loads(row["questions"] or "[]"), "pingRoleIds": json.loads(row["ping_role_ids"] or "[]"),
        })
    teams = []
    for row in team_rows:
        role = guild.get_role(row["team_role_id"])
        manager = guild.get_member(row["manager_id"])
        if role:
            teams.append({"roleId": str(role.id), "name": role.name, "managerId": str(row["manager_id"]), "managerName": manager.display_name if manager else "Unknown member", "players": len(get_team_players(role, row["manager_id"]))})
    return web.json_response({
        "guild": {"id": str(guild.id), "name": guild.name, "memberCount": guild.member_count},
        "channels": [{"id": str(channel.id), "name": channel.name, "type": "category" if isinstance(channel, discord.CategoryChannel) else "text"} for channel in guild.channels if isinstance(channel, (discord.TextChannel, discord.CategoryChannel))],
        "roles": [{"id": str(role.id), "name": role.name} for role in reversed(guild.roles) if not role.is_default()],
        "members": [{"id": str(member.id), "name": member.display_name} for member in guild.members if not member.bot],
        "settings": settings, "excludedRoleIds": excluded_roles, "gametimeRoleIds": gametime_roles,
        "tickets": tickets, "teams": teams,
    })


async def save_website_bot_config(request):
    if not current_web_admin(request):
        raise web.HTTPUnauthorized(text="Sign in required")
    guild = dashboard_guild()
    if not guild:
        raise web.HTTPServiceUnavailable(text="The configured Discord server is not available")
    try:
        payload = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest):
        raise web.HTTPBadRequest(text="Invalid dashboard settings")
    settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
    channel_ids = {str(channel.id) for channel in guild.channels}
    role_ids = {str(role.id) for role in guild.roles}
    channel_keys = {"signings_channel_id", "log_channel_id", "welcome_channel_id", "gametime_channel_id", "ticket_category_id", "totw_category_id"}
    role_keys = {"manager_role_id", "ticket_staff_role_id"}
    for key in BOT_DASHBOARD_SETTING_KEYS:
        value = str(settings.get(key, "")).strip()
        if key in channel_keys and value and value not in channel_ids:
            raise web.HTTPBadRequest(text=f"Invalid channel for {key}")
        if key in role_keys and value and value not in role_ids:
            raise web.HTTPBadRequest(text=f"Invalid role for {key}")
        if key == "roster_cap":
            value = str(max(1, min(99, int(value or ROSTER_CAP))))
        if key == "welcome_message":
            value = value[:1800]
        if key == "rolesaver_enabled":
            value = "true" if value.lower() == "true" else "false"
        set_guild_setting(guild, key, value)
    excluded = {str(value) for value in payload.get("excludedRoleIds", []) if str(value) in role_ids}
    gametime = {str(value) for value in payload.get("gametimeRoleIds", []) if str(value) in role_ids}
    tickets = payload.get("tickets", [])[:25]
    with connect_db() as db:
        db.execute("DELETE FROM rolesaver_excluded")
        db.executemany("INSERT INTO rolesaver_excluded (role_id) VALUES (?)", [(int(value),) for value in excluded])
        db.execute("DELETE FROM gametime_roles")
        db.executemany("INSERT INTO gametime_roles (role_id) VALUES (?)", [(int(value),) for value in gametime])
        db.execute("DELETE FROM ticket_types")
        for ticket in tickets:
            label = str(ticket.get("label", "Support")).strip()[:80] or "Support"
            emoji = str(ticket.get("emoji", "🎫"))[:20]
            style = str(ticket.get("style", "blue")).lower()
            if style not in {"blue", "primary", "grey", "gray", "secondary", "green", "success", "red", "danger"}:
                style = "blue"
            questions = [str(item).strip()[:45] for item in ticket.get("questions", []) if str(item).strip()][:5]
            pings = [int(value) for value in ticket.get("pingRoleIds", []) if str(value) in role_ids][:5]
            db.execute("INSERT INTO ticket_types (label, emoji, style, questions, ping_role_ids) VALUES (?, ?, ?, ?, ?)", (label, emoji, style, json.dumps(questions), json.dumps(pings)))
    return web.json_response({"saved": True})


async def website_bot_team(request):
    if not current_web_admin(request):
        raise web.HTTPUnauthorized(text="Sign in required")
    guild = dashboard_guild()
    if not guild:
        raise web.HTTPServiceUnavailable(text="The configured Discord server is not available")
    payload = await request.json()
    action = str(payload.get("action", ""))
    role = guild.get_role(int(payload.get("roleId", 0) or 0))
    if not role:
        raise web.HTTPBadRequest(text="Choose a valid team role")
    if action == "remove":
        with connect_db() as db:
            team = db.execute("SELECT manager_id FROM teams WHERE guild_id = ? AND team_role_id = ?", (guild.id, role.id)).fetchone()
            db.execute("DELETE FROM teams WHERE guild_id = ? AND team_role_id = ?", (guild.id, role.id))
            db.execute("DELETE FROM signed_players WHERE guild_id = ? AND team_role_id = ?", (guild.id, role.id))
        return web.json_response({"removed": True, "managerId": str(team["manager_id"]) if team else ""})
    manager = guild.get_member(int(payload.get("managerId", 0) or 0))
    if not manager:
        raise web.HTTPBadRequest(text="Choose a valid manager")
    manager_role_id = int(get_guild_setting(guild, "manager_role_id", str(MANAGER_ROLE_ID)) or 0)
    manager_role = guild.get_role(manager_role_id)
    if not manager_role:
        raise web.HTTPBadRequest(text="Configure the manager role first")
    with connect_db() as db:
        db.execute("DELETE FROM teams WHERE guild_id = ? AND (manager_id = ? OR team_role_id = ?)", (guild.id, manager.id, role.id))
        db.execute("INSERT INTO teams (team_role_id, manager_id, guild_id) VALUES (?, ?, ?)", (role.id, manager.id, guild.id))
    try:
        await manager.add_roles(manager_role, role, reason="Configured through APL web dashboard")
    except discord.Forbidden:
        raise web.HTTPForbidden(text="Move the bot role above the manager and team roles")
    return web.json_response({"added": True})


async def website_health(request):
    return web.json_response({"status": "ok", "bot": bot.user is not None})


async def start_website(client):
    app = web.Application(client_max_size=12 * 1024 * 1024)
    app.router.add_get("/", website_home)
    app.router.add_get("/dashboard", website_dashboard)
    app.router.add_get("/api/site-data", website_data)
    app.router.add_post("/api/site-data", save_website_data)
    app.router.add_get("/auth/verify", verify_admin_email)
    app.router.add_get("/api/me", website_admin_status)
    app.router.add_post("/api/logout", website_admin_logout)
    app.router.add_post("/api/admin/invite", invite_website_admin)
    app.router.add_get("/api/bot-config", website_bot_config)
    app.router.add_post("/api/bot-config", save_website_bot_config)
    app.router.add_post("/api/bot-team", website_bot_team)
    app.router.add_get("/health", website_health)
    app.router.add_get("/{filename}", website_asset)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    client.website_runner = runner
    print(f"APL website listening on port {WEB_PORT}")


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

        guild = bot.get_guild(offer["guild_id"])
        if guild is None:
            await reply(interaction, "I cannot find the configured server. Please contact an admin.")
            return

        member = guild.get_member(offer["player_id"])
        role = guild.get_role(offer["team_role_id"])
        if member is None or role is None:
            await reply(interaction, "The player or team role could not be found. Please contact an admin.")
            return

        roster_cap = get_roster_cap(guild)
        roster_count = len(get_team_players(role, offer["manager_id"]))
        if roster_count >= roster_cap:
            await reply(interaction, f"This team's roster is full ({roster_count}/{roster_cap}). Please contact the manager.")
            return

        try:
            await member.add_roles(role, reason="Player accepted a team offer")
        except discord.Forbidden:
            await reply(interaction, "I cannot assign that role. Put my bot role above the team role.")
            return

        with connect_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO signed_players (guild_id, team_role_id, player_id) VALUES (?, ?, ?)",
                (guild.id, role.id, member.id),
            )

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
        signings_channel_id = int(get_guild_setting(guild, "signings_channel_id", str(SIGNINGS_CHANNEL_ID)) or 0)
        channel = guild.get_channel(signings_channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(signings_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None

        if channel is not None and hasattr(channel, "send"):
            manager = guild.get_member(offer["manager_id"])
            manager_text = manager.mention if manager else f"<@{offer['manager_id']}>"
            roster_count = len(get_team_players(role, offer["manager_id"]))
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
                value=f"{roster_count}/{get_roster_cap(guild)}",
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
    return isinstance(interaction.user, discord.Member) and member_is_admin(interaction.user)


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
    staff_role_id = int(get_guild_setting(guild, "ticket_staff_role_id", str(TICKET_STAFF_ROLE_ID)) or 0)
    staff_role = guild.get_role(staff_role_id)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    try:
        ping_role_ids = json.loads(ticket_type["ping_role_ids"] or "[]")
    except (json.JSONDecodeError, TypeError):
        ping_role_ids = []
    ping_roles = [guild.get_role(int(role_id)) for role_id in ping_role_ids]
    ping_roles = [role for role in ping_roles if role is not None]
    for ping_role in ping_roles:
        overwrites[ping_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    category_id = int(get_guild_setting(guild, "ticket_category_id", str(TICKET_CATEGORY_ID)) or 0)
    category = guild.get_channel(category_id)
    safe_user = re.sub(r"[^a-z0-9-]", "", interaction.user.display_name.lower().replace(" ", "-"))[:25] or "member"
    safe_type = re.sub(r"[^a-z0-9-]", "", ticket_type["label"].lower().replace(" ", "-"))[:20] or "support"
    channel = await guild.create_text_channel(
        f"{safe_type}-{safe_user}",
        category=category if isinstance(category, discord.CategoryChannel) else None,
        overwrites=overwrites,
        topic=f"apl-ticket-owner:{interaction.user.id}",
        reason=f"{ticket_type['label']} ticket opened",
    )
    roles_to_ping = list(dict.fromkeys(([staff_role] if staff_role else []) + ping_roles))
    staff_mentions = " ".join(role.mention for role in roles_to_ping) or "Server administrators"
    embed = discord.Embed(
        title="Ticket Opened",
        description=f"{interaction.user.mention} has created a new **{ticket_type['label']}** ticket.",
        color=discord.Color.purple(),
    )
    for question, answer in answers or []:
        embed.add_field(name=question, value=answer or "No answer provided", inline=False)
    embed.set_footer(text="APL Ticket System • Use the buttons below")
    await channel.send(
        content=f"{interaction.user.mention} {staff_mentions}",
        embed=embed,
        view=TicketControlsView(),
        allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
    )
    await reply(interaction, f"Your ticket is ready: {channel.mention}")
    await send_log(guild, "Ticket Opened", f"{interaction.user.mention} opened a **{ticket_type['label']}** ticket: {channel.mention}", discord.Color.green())


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
        await send_log(interaction.guild, "Ticket Closed", f"{interaction.channel.mention} was closed.", discord.Color.red())
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


@bot.event
async def on_member_remove(member):
    if get_guild_setting(member.guild, "rolesaver_enabled", "false") != "true":
        return
    with connect_db() as db:
        excluded = {row["role_id"] for row in db.execute("SELECT role_id FROM rolesaver_excluded").fetchall()}
        role_ids = [role.id for role in member.roles if role != member.guild.default_role and not role.managed and role.id not in excluded]
        db.execute("INSERT OR REPLACE INTO saved_member_roles (user_id, role_ids) VALUES (?, ?)", (member.id, json.dumps(role_ids)))
    await send_log(member.guild, "Member Left", f"{member.mention} (`{member.id}`) left. {len(role_ids)} roles were saved.", discord.Color.orange())


@bot.event
async def on_member_join(member):
    if get_guild_setting(member.guild, "rolesaver_enabled", "false") == "true":
        with connect_db() as db:
            saved = db.execute("SELECT role_ids FROM saved_member_roles WHERE user_id = ?", (member.id,)).fetchone()
        if saved:
            roles = [member.guild.get_role(role_id) for role_id in json.loads(saved["role_ids"])]
            roles = [role for role in roles if role and not role.managed]
            if roles:
                try:
                    await member.add_roles(*roles, reason="APL role saver restored member roles")
                except discord.Forbidden:
                    pass
    channel_id = int(get_guild_setting(member.guild, "welcome_channel_id", "0") or 0)
    channel = member.guild.get_channel(channel_id)
    if channel:
        template = get_guild_setting(member.guild, "welcome_message", "Welcome {user} to **{server}**!")
        message = template.replace("{user}", member.mention).replace("{server}", member.guild.name).replace("{member_count}", str(member.guild.member_count))
        embed = discord.Embed(title="Welcome!", description=message, color=discord.Color.green())
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)
    await send_log(member.guild, "Member Joined", f"{member.mention} (`{member.id}`) joined the server.", discord.Color.green())


@bot.event
async def on_member_ban(guild, user):
    await asyncio.sleep(1)
    reason = "No reason provided"
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
            if entry.target.id == user.id:
                reason = entry.reason or reason
                break
    except discord.Forbidden:
        pass
    await send_log(guild, "Member Banned", f"{user.mention} was banned.\n\n**Reason:** {reason}", discord.Color.red())


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    await bot.process_commands(message)
    if message.content.startswith("?"):
        return
    with connect_db() as db:
        sticky = db.execute("SELECT * FROM sticky_messages WHERE channel_id = ?", (message.channel.id,)).fetchone()
    if not sticky:
        return
    if sticky["message_id"]:
        try:
            old_message = await message.channel.fetch_message(sticky["message_id"])
            await old_message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
    embed = discord.Embed(description=sticky["content"], color=discord.Color.gold())
    embed.set_footer(text="📌 Sticky message")
    new_message = await message.channel.send(embed=embed)
    with connect_db() as db:
        db.execute("UPDATE sticky_messages SET message_id = ? WHERE channel_id = ?", (new_message.id, message.channel.id))


@bot.command(name="stick")
async def stick(ctx, *, message: str):
    if not member_is_admin(ctx.author):
        return await ctx.reply("Only bot administrators can create sticky messages.")
    embed = discord.Embed(description=message, color=discord.Color.gold())
    embed.set_footer(text="📌 Sticky message")
    sticky_message = await ctx.send(embed=embed)
    with connect_db() as db:
        db.execute("INSERT OR REPLACE INTO sticky_messages (channel_id, content, message_id) VALUES (?, ?, ?)", (ctx.channel.id, message, sticky_message.id))
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    await send_log(ctx.guild, "Sticky Message Set", f"{ctx.author.mention} set a sticky message in {ctx.channel.mention}.")


@bot.command(name="unstick")
async def unstick(ctx):
    if not member_is_admin(ctx.author):
        return await ctx.reply("Only bot administrators can remove sticky messages.")
    with connect_db() as db:
        sticky = db.execute("SELECT message_id FROM sticky_messages WHERE channel_id = ?", (ctx.channel.id,)).fetchone()
        db.execute("DELETE FROM sticky_messages WHERE channel_id = ?", (ctx.channel.id,))
    if sticky and sticky["message_id"]:
        try:
            old_message = await ctx.channel.fetch_message(sticky["message_id"])
            await old_message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
    await ctx.reply("Sticky message removed.", delete_after=5)


@bot.tree.command(name="websiteaccess", description="Get the website owner's private sign-in link")
async def websiteaccess(interaction: discord.Interaction):
    if not is_bot_owner(interaction.user.id):
        await reply(interaction, "Only a global bot owner can create the website owner link.")
        return
    if not WEB_OWNER_EMAIL or not WEB_BASE_URL:
        await reply(interaction, "Set `WEB_OWNER_EMAIL` and `WEB_BASE_URL` in Railway first.")
        return
    try:
        login_url = create_website_login_url(WEB_OWNER_EMAIL)
    except RuntimeError as error:
        await reply(interaction, str(error))
        return
    embed = discord.Embed(
        title="Private Website Access",
        description=f"[Click here to open the admin website]({login_url})\n\nThis private link expires in 30 minutes and can only be used once.",
        color=discord.Color.green(),
    )
    await reply(interaction, embed)


@bot.tree.command(name="dashboard", description="Open your private APL Bot control dashboard")
async def dashboard_command(interaction: discord.Interaction):
    if not is_bot_owner(interaction.user.id) and not is_saved_admin(interaction.user.id, interaction.guild):
        await reply(interaction, "Only the server owner or a configured bot administrator can open the dashboard.")
        return
    if not WEB_OWNER_EMAIL or not WEB_BASE_URL:
        await reply(interaction, "Set `WEB_OWNER_EMAIL` and `WEB_BASE_URL` in Railway first.")
        return
    try:
        login_url = create_website_login_url(WEB_OWNER_EMAIL)
    except RuntimeError as error:
        await reply(interaction, str(error))
        return
    embed = discord.Embed(
        title="APL Bot Dashboard",
        description=f"[Open your private bot dashboard]({login_url})\n\nThis sign-in link expires in 30 minutes and can only be used once.",
        color=discord.Color.red(),
    )
    embed.set_footer(text="Only you can see this message")
    await reply(interaction, embed)


@bot.tree.command(name="addteam", description="Add a team and assign its manager")
@app_commands.describe(
    manager="The team's manager",
    team_role="The team's Discord role and website name",
    logo="Optional PNG, JPG, or WebP team logo for the website",
)
async def addteam(
    interaction: discord.Interaction,
    manager: discord.Member,
    team_role: discord.Role,
    logo: discord.Attachment | None = None,
):
    if not is_saved_admin(interaction.user.id, interaction.guild):
        await reply(interaction, "Only a configured bot administrator can add teams.")
        return

    manager_role_id = int(get_guild_setting(interaction.guild, "manager_role_id", str(MANAGER_ROLE_ID)) or 0)
    manager_role = interaction.guild.get_role(manager_role_id)
    if manager_role is None:
        await reply(interaction, "The manager role is not configured. An admin must run `/setmanagerrole` first.")
        return

    logo_data = None
    if logo is not None:
        allowed_types = {"image/png", "image/jpeg", "image/webp", "image/gif"}
        if logo.content_type not in allowed_types:
            await reply(interaction, "The team logo must be a PNG, JPG, WebP, or GIF image.")
            return
        if logo.size > 3 * 1024 * 1024:
            await reply(interaction, "The team logo must be smaller than 3 MB.")
            return
        try:
            logo_bytes = await logo.read()
        except discord.HTTPException:
            await reply(interaction, "I could not download that logo. Please try uploading it again.")
            return
        logo_data = f"data:{logo.content_type};base64,{base64.b64encode(logo_bytes).decode('ascii')}"

    previous_team = get_team_for_manager(manager.id, interaction.guild.id)
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
        db.execute("DELETE FROM teams WHERE manager_id = ? AND guild_id = ?", (manager.id, interaction.guild.id))
        db.execute(
            "INSERT OR REPLACE INTO teams (team_role_id, manager_id, guild_id) VALUES (?, ?, ?)",
            (team_role.id, manager.id, interaction.guild.id),
        )

    raw_site_data = get_setting("website_data", "{}") or "{}"
    try:
        site_data = json.loads(raw_site_data)
    except json.JSONDecodeError:
        site_data = {}
    site_teams = site_data.get("teams", [])
    if not isinstance(site_teams, list):
        site_teams = []
    website_team = next(
        (team for team in site_teams if str(team.get("discordRoleId", "")) == str(team_role.id)),
        None,
    )
    if website_team is None:
        website_team = {
            "discordRoleId": str(team_role.id),
            "name": team_role.name,
            "managerId": str(manager.id),
            "managerName": manager.display_name,
            "p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0,
        }
        site_teams.append(website_team)
    else:
        website_team["name"] = team_role.name
        website_team["managerId"] = str(manager.id)
        website_team["managerName"] = manager.display_name
    if logo_data:
        website_team["logo"] = logo_data
    site_data["teams"] = site_teams
    set_setting("website_data", json.dumps(site_data))

    logo_message = " with its website logo" if logo_data else ""
    await reply(interaction, f"Added {team_role.mention} with {manager.mention} as manager{logo_message}. It is now listed on the website.")
    await send_log(interaction.guild, "Team Added", f"**Team:** {team_role.mention}\n**Manager:** {manager.mention}", discord.Color.green())


@bot.tree.command(name="removeteam", description="Remove a team from the bot and website")
@app_commands.describe(team_role="The team role to remove")
async def removeteam(interaction: discord.Interaction, team_role: discord.Role):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can remove teams.")
        return
    with connect_db() as db:
        saved_team = db.execute(
            "SELECT manager_id FROM teams WHERE guild_id = ? AND team_role_id = ?",
            (interaction.guild.id, team_role.id),
        ).fetchone()
        if not saved_team:
            await reply(interaction, "That role is not registered as a team.")
            return
        db.execute(
            "DELETE FROM teams WHERE guild_id = ? AND team_role_id = ?",
            (interaction.guild.id, team_role.id),
        )
        db.execute(
            "DELETE FROM signed_players WHERE guild_id = ? AND team_role_id = ?",
            (interaction.guild.id, team_role.id),
        )
        db.execute(
            "UPDATE offers SET status = 'cancelled' WHERE guild_id = ? AND team_role_id = ? AND status = 'pending'",
            (interaction.guild.id, team_role.id),
        )

    raw_site_data = get_setting("website_data", "{}") or "{}"
    try:
        site_data = json.loads(raw_site_data)
    except json.JSONDecodeError:
        site_data = {}
    teams_data = site_data.get("teams", [])
    if isinstance(teams_data, list):
        site_data["teams"] = [
            team for team in teams_data
            if str(team.get("discordRoleId", "")) != str(team_role.id)
            and str(team.get("name", "")).casefold() != team_role.name.casefold()
        ]
        set_setting("website_data", json.dumps(site_data))

    await reply(interaction, f"Removed **{team_role.name}** from the bot and website. The Discord role was not deleted.")
    await send_log(interaction.guild, "Team Removed", f"**Team:** {team_role.mention}\n**Manager:** <@{saved_team['manager_id']}>", discord.Color.red())


@bot.tree.command(name="offer", description="Send a player an offer to join your team")
@app_commands.describe(player="The player you want to offer")
async def offer(interaction: discord.Interaction, player: discord.Member):
    if not await require_signings_channel(interaction):
        return
    team = get_team_for_manager(interaction.user.id, interaction.guild.id)
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
            "INSERT INTO offers (message_id, player_id, team_role_id, manager_id, guild_id) VALUES (?, ?, ?, ?, ?)",
            (dm_message.id, player.id, role.id, interaction.user.id, interaction.guild.id),
        )
    bot.add_view(view, message_id=dm_message.id)
    await reply(interaction, f"Offer sent to {player.mention}.")
    await send_log(interaction.guild, "Offer Sent", f"{interaction.user.mention} sent {player.mention} an offer for {role.mention}.")


@bot.tree.command(name="release", description="Remove a player from your team")
@app_commands.describe(player="The player you want to release")
async def release(interaction: discord.Interaction, player: discord.Member):
    if not await require_signings_channel(interaction):
        return
    team = get_team_for_manager(interaction.user.id, interaction.guild.id)
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
    with connect_db() as db:
        db.execute(
            "DELETE FROM signed_players WHERE guild_id = ? AND team_role_id = ? AND player_id = ?",
            (interaction.guild.id, role.id, player.id),
        )
    await reply(interaction, f"Released {player.mention} from {role.mention}.")
    await send_log(interaction.guild, "Player Released", f"{player.mention} was released from {role.mention}.", discord.Color.orange())


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
    if get_team_for_manager(interaction.user.id, interaction.guild.id):
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
    if is_saved_admin(interaction.user.id, interaction.guild) or is_admin(interaction):
        embed.add_field(
            name="OWNER / ADMIN COMMANDS",
            value=(
                "`/addteam` · `/removeteam` · `/ownerlist` · `/ticketpanel`\n"
                "`/addticketbutton` · `/deleteticketbutton` · `/setticketpings`\n"
                "`/clearticketpings` · `/totw`\n"
                "`/allrosters` · `/rules` · `/managerlist` · `/poll`\n"
                "`/warn` · `/mute` · `/unmute` · `/kick` · `/ban` · `/purge`\n"
                "`/setwelcome` · `/rolesaver` · `/setlogchannel`\n"
                "`/setsigningschannel` · `/setrostercap` · `/setmanagerrole`\n"
                "`/websiteaccess` — private website owner login\n"
                "`/setgametimechannel` · `/gametimerole` · `/gametimeroles`\n"
                "`?stick message` · `?unstick`"
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
    members = get_team_players(role, team["manager_id"])
    embed = discord.Embed(title=role.name, description="APL Team Information", color=role.color if role.color.value else discord.Color.blurple())
    embed.add_field(name="Manager", value=manager.mention if manager else f"<@{team['manager_id']}>")
    embed.add_field(name="Roster", value=f"{len(members)}/{get_roster_cap(interaction.guild)}")
    embed.add_field(name="Team Role", value=role.mention, inline=False)
    if isinstance(role.display_icon, discord.Asset):
        embed.set_thumbnail(url=role.display_icon.url)
    await reply(interaction, embed)


@bot.tree.command(name="teams", description="List all registered teams")
async def teams(interaction: discord.Interaction):
    with connect_db() as db:
        saved_teams = db.execute("SELECT * FROM teams WHERE guild_id = ? ORDER BY team_role_id", (interaction.guild.id,)).fetchall()
    lines = []
    for team in saved_teams:
        role = interaction.guild.get_role(team["team_role_id"])
        if role:
            lines.append(f"• {role.mention} — {len(get_team_players(role, team['manager_id']))}/{get_roster_cap(interaction.guild)} — Manager <@{team['manager_id']}>")
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
    members = get_team_players(role, team["manager_id"])
    roster_text = "\n".join(f"• {member.mention}" for member in members) or "No players"
    embed = discord.Embed(title=f"{role.name} Roster", description=roster_text, color=role.color if role.color.value else discord.Color.blurple())
    embed.set_footer(text=f"{len(members)}/{get_roster_cap(interaction.guild)} roster places used")
    await reply(interaction, embed)


@bot.tree.command(name="ownerlist", description="View the bot's owner configuration")
@app_commands.default_permissions(administrator=True)
async def ownerlist(interaction: discord.Interaction):
    if not is_bot_owner(interaction.user.id) and interaction.user.id != interaction.guild.owner_id:
        await reply(interaction, "Only this server's owner or a global bot owner can view this.")
        return
    with connect_db() as db:
        server_admins = db.execute("SELECT user_id FROM guild_admins WHERE guild_id = ?", (interaction.guild.id,)).fetchall()
    owners = "\n".join(f"• <@{owner_id}> (`{owner_id}`)" for owner_id in OWNER_IDS) or "None configured"
    admins = "\n".join(f"• <@{row['user_id']}>" for row in server_admins) or "No additional admins"
    embed = discord.Embed(title="APL Bot Administration", color=discord.Color.gold())
    embed.add_field(name="Server Owner", value=f"<@{interaction.guild.owner_id}>", inline=False)
    embed.add_field(name="Server Bot Admins", value=admins, inline=False)
    embed.add_field(name="Global Bot Owners", value=owners, inline=False)
    await reply(interaction, embed)


@bot.tree.command(name="canceloffer", description="Cancel a pending offer sent to a player")
@app_commands.describe(player="Player whose offer you want to cancel")
async def canceloffer(interaction: discord.Interaction, player: discord.Member):
    if not await require_signings_channel(interaction):
        return
    team = get_team_for_manager(interaction.user.id, interaction.guild.id)
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
    if not await require_signings_channel(interaction):
        return
    team = get_team_for_manager(interaction.user.id, interaction.guild.id)
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
    if not await require_signings_channel(interaction):
        return
    team = get_team_for_manager(interaction.user.id, interaction.guild.id)
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
    if not await require_signings_channel(interaction):
        return
    team = get_team_for_manager(interaction.user.id, interaction.guild.id)
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


@bot.tree.command(name="addadmin", description="Allow a member to use APL admin commands")
@app_commands.describe(member="Member to make a bot administrator")
async def addadmin(interaction: discord.Interaction, member: discord.Member):
    if not is_bot_owner(interaction.user.id) and interaction.user.id != interaction.guild.owner_id:
        await reply(interaction, "Only this server's owner or a global bot owner can add bot administrators.")
        return
    with connect_db() as db:
        db.execute("INSERT OR IGNORE INTO guild_admins (guild_id, user_id) VALUES (?, ?)", (interaction.guild.id, member.id))
    await reply(interaction, f"{member.mention} can now use APL admin commands.")
    await send_log(interaction.guild, "Bot Admin Added", f"{member.mention} was added as a bot administrator.", discord.Color.green())


@bot.tree.command(name="removeadmin", description="Remove a member's APL admin access")
@app_commands.describe(member="Bot administrator to remove")
async def removeadmin(interaction: discord.Interaction, member: discord.Member):
    if not is_bot_owner(interaction.user.id) and interaction.user.id != interaction.guild.owner_id:
        await reply(interaction, "Only this server's owner or a global bot owner can remove bot administrators.")
        return
    if member.id == interaction.guild.owner_id or is_bot_owner(member.id):
        await reply(interaction, "The server owner and global bot owners cannot be removed.")
        return
    with connect_db() as db:
        db.execute("DELETE FROM guild_admins WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, member.id))
    await reply(interaction, f"Removed APL admin access from {member.mention}.")
    await send_log(interaction.guild, "Bot Admin Removed", f"{member.mention} was removed as a bot administrator.", discord.Color.orange())


@bot.tree.command(name="setlogchannel", description="Choose where the bot posts embed logs")
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can change the log channel.")
        return
    set_guild_setting(interaction.guild, "log_channel_id", channel.id)
    await reply(interaction, f"Bot logs will now be posted in {channel.mention}.")
    await send_log(interaction.guild, "Log Channel Configured", f"All APL Bot logs will be posted in {channel.mention}.", discord.Color.green())


@bot.tree.command(name="setsigningschannel", description="Choose where accepted signing embeds are posted")
async def setsigningschannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can change the signings channel.")
        return
    set_guild_setting(interaction.guild, "signings_channel_id", channel.id)
    await reply(interaction, f"Accepted signing embeds will now be posted in {channel.mention}.")
    await send_log(interaction.guild, "Signings Channel Configured", f"Signing announcements will be posted in {channel.mention}.", discord.Color.green())


@bot.tree.command(name="setrostercap", description="Set the maximum number of players on each team")
async def setrostercap(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 99]):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can change the roster cap.")
        return
    set_guild_setting(interaction.guild, "roster_cap", amount)
    await reply(interaction, f"The roster cap is now **{amount} players per team**. Managers are not counted as players.")
    await send_log(interaction.guild, "Roster Cap Updated", f"The roster cap is now **{amount} players**.", discord.Color.green())


@bot.tree.command(name="setmanagerrole", description="Choose the role assigned to registered team managers")
async def setmanagerrole(interaction: discord.Interaction, role: discord.Role):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can change the manager role.")
        return
    set_guild_setting(interaction.guild, "manager_role_id", role.id)
    await reply(interaction, f"The manager role is now {role.mention}.")
    await send_log(interaction.guild, "Manager Role Updated", f"The manager role is now {role.mention}.", discord.Color.green())


@bot.tree.command(name="allrosters", description="View every team, manager and player roster")
async def allrosters(interaction: discord.Interaction):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can view all rosters.")
        return
    with connect_db() as db:
        saved_teams = db.execute("SELECT * FROM teams WHERE guild_id = ?", (interaction.guild.id,)).fetchall()
    embeds = []
    for team in saved_teams:
        role = interaction.guild.get_role(team["team_role_id"])
        if not role:
            continue
        players = get_team_players(role, team["manager_id"])
        embed = discord.Embed(title=f"{role.name} Roster", color=role.color if role.color.value else discord.Color.blurple())
        embed.add_field(name="Manager", value=f"<@{team['manager_id']}>", inline=False)
        embed.add_field(name=f"Players — {len(players)}/{get_roster_cap(interaction.guild)}", value="\n".join(member.mention for member in players) or "No players", inline=False)
        embeds.append(embed)
    if not embeds:
        await reply(interaction, "No teams have been configured.")
        return
    await interaction.response.send_message(embed=embeds[0], ephemeral=True)
    for embed in embeds[1:10]:
        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="rules", description="Post your exact rules in a selected channel")
@app_commands.describe(channel="Channel where the rules will be posted", title="Rules embed title", rules="Exact rules text to post")
async def rules(interaction: discord.Interaction, channel: discord.TextChannel, rules: str, title: str = "Server Rules"):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can post rules.")
        return
    embed = discord.Embed(title=title, description=rules, color=discord.Color.gold())
    embed.set_footer(text=f"{interaction.guild.name} • Please follow all rules")
    await channel.send(embed=embed)
    await reply(interaction, f"Rules posted in {channel.mention}.")
    await send_log(interaction.guild, "Rules Posted", f"Rules were posted in {channel.mention}.")


@bot.tree.command(name="managerlist", description="Post your manager-list message in a selected channel")
@app_commands.describe(channel="Channel where the manager list will be posted", message="Exact manager-list text")
async def managerlist(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can post the manager list.")
        return
    embed = discord.Embed(title="APL Manager List", description=message, color=discord.Color.blurple())
    embed.set_footer(text="APL League Management")
    await channel.send(embed=embed)
    await reply(interaction, f"Manager list posted in {channel.mention}.")
    await send_log(interaction.guild, "Manager List Posted", f"The manager list was posted in {channel.mention}.")


@bot.tree.command(name="setwelcome", description="Configure the welcome embed")
@app_commands.describe(channel="Welcome channel", message="Use {user}, {server}, and {member_count}")
async def setwelcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can configure welcomes.")
        return
    set_guild_setting(interaction.guild, "welcome_channel_id", channel.id)
    set_guild_setting(interaction.guild, "welcome_message", message)
    await reply(interaction, f"Welcome messages will be posted in {channel.mention}.")


@bot.tree.command(name="rolesaver", description="Turn automatic role saving on or off")
async def rolesaver(interaction: discord.Interaction, enabled: bool):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can configure role saver.")
        return
    set_guild_setting(interaction.guild, "rolesaver_enabled", "true" if enabled else "false")
    await reply(interaction, f"Role saver is now **{'enabled' if enabled else 'disabled'}**.")


@bot.tree.command(name="rolesaverexclude", description="Choose a role that should not be restored")
async def rolesaverexclude(interaction: discord.Interaction, role: discord.Role):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can configure role saver.")
        return
    with connect_db() as db:
        db.execute("INSERT OR IGNORE INTO rolesaver_excluded (role_id) VALUES (?)", (role.id,))
    await reply(interaction, f"{role.mention} will not be restored when members rejoin.")


@bot.tree.command(name="rolesaverallow", description="Allow a previously excluded role to be restored")
async def rolesaverallow(interaction: discord.Interaction, role: discord.Role):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can configure role saver.")
        return
    with connect_db() as db:
        db.execute("DELETE FROM rolesaver_excluded WHERE role_id = ?", (role.id,))
    await reply(interaction, f"{role.mention} can now be restored when members rejoin.")


@bot.tree.command(name="warn", description="Warn a member")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can warn members.")
        return
    with connect_db() as db:
        db.execute("INSERT INTO warnings (user_id, moderator_id, reason) VALUES (?, ?, ?)", (member.id, interaction.user.id, reason))
    try:
        await member.send(embed=discord.Embed(title=f"Warning from {interaction.guild.name}", description=reason, color=discord.Color.orange()))
    except discord.Forbidden:
        pass
    await reply(interaction, f"Warned {member.mention}.")
    await send_log(interaction.guild, "Member Warned", f"{member.mention}\n**Reason:** {reason}", discord.Color.orange())


@bot.tree.command(name="mute", description="Temporarily mute a member")
async def mute(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can mute members.")
        return
    await member.timeout(discord.utils.utcnow() + __import__('datetime').timedelta(minutes=minutes), reason=reason)
    await reply(interaction, f"Muted {member.mention} for {minutes} minutes.")
    await send_log(interaction.guild, "Member Muted", f"{member.mention} for **{minutes} minutes**\n**Reason:** {reason}", discord.Color.orange())


@bot.tree.command(name="unmute", description="Remove a member's timeout")
async def unmute(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can unmute members.")
        return
    await member.timeout(None, reason=f"Unmuted by APL admin {interaction.user}")
    await reply(interaction, f"Unmuted {member.mention}.")
    await send_log(interaction.guild, "Member Unmuted", member.mention, discord.Color.green())


@bot.tree.command(name="kick", description="Kick a member from the server")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can kick members.")
        return
    await member.kick(reason=reason)
    await reply(interaction, f"Kicked {member}.")
    await send_log(interaction.guild, "Member Kicked", f"{member.mention}\n**Reason:** {reason}", discord.Color.red())


@bot.tree.command(name="ban", description="Ban a member from the server")
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can ban members.")
        return
    await member.ban(reason=reason)
    await reply(interaction, f"Banned {member}. The public log will show the member and reason only.")


@bot.tree.command(name="purge", description="Delete a number of recent messages")
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can purge messages.")
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)
    await send_log(interaction.guild, "Messages Purged", f"**{len(deleted)} messages** were removed from {interaction.channel.mention}.", discord.Color.orange())


@bot.tree.command(name="poll", description="Create a poll with up to five choices")
async def poll(interaction: discord.Interaction, question: str, option1: str, option2: str, option3: str = "", option4: str = "", option5: str = ""):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can create polls.")
        return
    options = [item for item in (option1, option2, option3, option4, option5) if item]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    description = "\n\n".join(f"{emojis[index]}  {option}" for index, option in enumerate(options))
    embed = discord.Embed(title=f"📊 {question}", description=description, color=discord.Color.blurple())
    embed.set_footer(text="React below to vote")
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    for emoji in emojis[:len(options)]:
        await message.add_reaction(emoji)
    await send_log(interaction.guild, "Poll Created", f"A poll was created in {interaction.channel.mention}." )


@bot.tree.command(name="setgametimechannel", description="Choose where game-time announcements are posted")
async def setgametimechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can choose the game-time channel.")
        return
    set_guild_setting(interaction.guild, "gametime_channel_id", channel.id)
    await reply(interaction, f"Game-time announcements will be posted in {channel.mention}.")
    await send_log(interaction.guild, "Game-Time Channel Set", f"Game-time announcements will be posted in {channel.mention}.", discord.Color.green())


@bot.tree.command(name="gametimerole", description="Allow or remove a role's access to /gametime")
@app_commands.describe(role="Role to configure", allowed="True to allow the role, false to remove access")
async def gametimerole(interaction: discord.Interaction, role: discord.Role, allowed: bool):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can configure game-time roles.")
        return
    with connect_db() as db:
        if allowed:
            db.execute("INSERT OR IGNORE INTO gametime_roles (role_id) VALUES (?)", (role.id,))
        else:
            db.execute("DELETE FROM gametime_roles WHERE role_id = ?", (role.id,))
    action = "can now" if allowed else "can no longer"
    await reply(interaction, f"{role.mention} {action} use `/gametime`.")
    await send_log(interaction.guild, "Game-Time Access Updated", f"{role.mention} {action} use `/gametime`.")


@bot.tree.command(name="gametimeroles", description="View the roles allowed to use /gametime")
async def gametimeroles(interaction: discord.Interaction):
    if not is_admin(interaction):
        await reply(interaction, "Only bot administrators can view this configuration.")
        return
    with connect_db() as db:
        role_ids = [row["role_id"] for row in db.execute("SELECT role_id FROM gametime_roles").fetchall()]
    roles = [interaction.guild.get_role(role_id) for role_id in role_ids]
    text = "\n".join(f"• {role.mention}" for role in roles if role) or "No roles have access yet."
    embed = discord.Embed(title="Game-Time Command Roles", description=text, color=discord.Color.blurple())
    await reply(interaction, embed)


@bot.tree.command(name="gametime", description="Post your scheduled game time")
@app_commands.describe(opponent="Team you are playing", date="Game date, for example 20 August", time="Game time including timezone, for example 8:00 PM UK", note="Optional extra information")
async def gametime(interaction: discord.Interaction, opponent: str, date: str, time: str, note: str = ""):
    with connect_db() as db:
        allowed_role_ids = {row["role_id"] for row in db.execute("SELECT role_id FROM gametime_roles").fetchall()}
    member_role_ids = {role.id for role in interaction.user.roles}
    if not is_admin(interaction) and not allowed_role_ids.intersection(member_role_ids):
        await reply(interaction, "You do not have a role that can use `/gametime`.")
        return
    channel_id = int(get_guild_setting(interaction.guild, "gametime_channel_id", "0") or 0)
    channel = interaction.guild.get_channel(channel_id)
    if not channel:
        await reply(interaction, "The game-time channel has not been configured. Ask an admin to use `/setgametimechannel`.")
        return
    team = get_team_for_member(interaction.user)
    team_role = interaction.guild.get_role(team["team_role_id"]) if team else None
    embed = discord.Embed(
        title="⚽ Game Time Confirmed",
        description="A new fixture time has been submitted.",
        color=team_role.color if team_role and team_role.color.value else discord.Color.green(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Team", value=team_role.mention if team_role else interaction.user.mention, inline=True)
    embed.add_field(name="Opponent", value=opponent, inline=True)
    embed.add_field(name="Date", value=date, inline=True)
    embed.add_field(name="Kick-Off", value=time, inline=True)
    embed.add_field(name="Submitted By", value=interaction.user.mention, inline=True)
    if note:
        embed.add_field(name="Additional Information", value=note, inline=False)
    embed.set_footer(text="APL Fixtures • Please arrive before kick-off")
    await channel.send(embed=embed)
    await reply(interaction, f"Your game time was posted in {channel.mention}.")
    await send_log(interaction.guild, "Game Time Posted", f"{interaction.user.mention} posted a fixture against **{opponent}** for **{date} at {time}**.", discord.Color.green())


@bot.tree.command(name="ticketpanel", description="Post the APL support ticket panel")
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


@bot.tree.command(name="setticketpings", description="Choose roles pinged when a ticket type is opened")
@app_commands.describe(
    name="Exact ticket button name",
    role1="First role to ping",
    role2="Optional second role",
    role3="Optional third role",
    role4="Optional fourth role",
    role5="Optional fifth role",
)
async def setticketpings(
    interaction: discord.Interaction,
    name: str,
    role1: discord.Role,
    role2: discord.Role | None = None,
    role3: discord.Role | None = None,
    role4: discord.Role | None = None,
    role5: discord.Role | None = None,
):
    if not is_admin(interaction):
        await reply(interaction, "Only server administrators can configure ticket pings.")
        return
    roles = list(dict.fromkeys(role.id for role in (role1, role2, role3, role4, role5) if role))
    with connect_db() as db:
        cursor = db.execute(
            "UPDATE ticket_types SET ping_role_ids = ? WHERE lower(label) = lower(?)",
            (json.dumps(roles), name),
        )
    if not cursor.rowcount:
        await reply(interaction, f"I could not find a ticket button named **{name}**.")
        return
    mentions = " ".join(f"<@&{role_id}>" for role_id in roles)
    await reply(interaction, f"Tickets opened with **{name}** will ping: {mentions}")


@bot.tree.command(name="clearticketpings", description="Remove configured ping roles from a ticket type")
async def clearticketpings(interaction: discord.Interaction, name: str):
    if not is_admin(interaction):
        await reply(interaction, "Only server administrators can configure ticket pings.")
        return
    with connect_db() as db:
        cursor = db.execute(
            "UPDATE ticket_types SET ping_role_ids = '[]' WHERE lower(label) = lower(?)",
            (name,),
        )
    await reply(interaction, f"Cleared ping roles for **{name}**." if cursor.rowcount else f"I could not find a ticket button named **{name}**.")


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
    totw_category_id = int(get_guild_setting(guild, "totw_category_id", str(TOTW_CATEGORY_ID)) or 0)
    category = guild.get_channel(totw_category_id)
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
