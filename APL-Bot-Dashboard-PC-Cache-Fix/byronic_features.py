import asyncio
import json
import math
import random
import re
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands


BRAND_COLOR = 0x5865F2
SUCCESS_COLOR = 0x3BA55D
ERROR_COLOR = 0xED4245


def brand_embed(title=None, description="", *, color=BRAND_COLOR, timestamp=None, **kwargs):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=timestamp or discord.utils.utcnow(), **kwargs)
    embed.set_footer(text="APL Bot • Server Management")
    return embed


def parse_duration(value):
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value.lower())
    if not match:
        return None
    number, unit = int(match.group(1)), match.group(2)
    return number * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def level_from_xp(xp):
    return int(math.sqrt(max(0, xp) / 100))


class ByronicFeatures(commands.Cog):
    def __init__(self, bot, connect_db, is_admin, get_setting, set_setting, reply, send_log):
        self.bot = bot
        self.connect_db = connect_db
        self.is_admin = is_admin
        self.get_setting = get_setting
        self.set_setting = set_setting
        self.reply = reply
        self.send_log = send_log
        self.invite_cache = {}
        self.message_cooldowns = {}
        self.spam_cache = {}
        self._setup_database()

    def _setup_database(self):
        with self.connect_db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS moderation_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL, action TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS warning_points (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    case_id INTEGER NOT NULL, points INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (guild_id, case_id)
                );
                CREATE TABLE IF NOT EXISTS member_stats (
                    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    xp INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
                    invites INTEGER NOT NULL DEFAULT 0, bonus_invites INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS level_rewards (
                    guild_id INTEGER NOT NULL, level INTEGER NOT NULL, role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, level)
                );
                CREATE TABLE IF NOT EXISTS ignored_items (
                    guild_id INTEGER NOT NULL, module TEXT NOT NULL, item_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, module, item_type, item_id)
                );
                CREATE TABLE IF NOT EXISTS giveaways (
                    message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL, prize TEXT NOT NULL, winners INTEGER NOT NULL,
                    ends_at INTEGER NOT NULL, ended INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS starboard_posts (
                    source_message_id INTEGER PRIMARY KEY,
                    starboard_message_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL
                );
                """
            )

    def setting(self, guild, key, default=""):
        return self.get_setting(f"guild:{guild.id}:byronic:{key}", default)

    def save_setting(self, guild, key, value):
        self.set_setting(f"guild:{guild.id}:byronic:{key}", value)

    async def require_admin(self, interaction):
        if self.is_admin(interaction):
            return True
        await self.reply(interaction, brand_embed("Permission Denied", "Only server administrators can use this command.", color=ERROR_COLOR))
        return False

    def add_case(self, guild, user, moderator, action, reason, points=0):
        with self.connect_db() as db:
            cursor = db.execute(
                "INSERT INTO moderation_cases (guild_id,user_id,moderator_id,action,reason) VALUES (?,?,?,?,?)",
                (guild.id, user.id, moderator.id, action, reason or "No reason provided"),
            )
            case_id = cursor.lastrowid
            if points:
                db.execute(
                    "INSERT INTO warning_points (guild_id,user_id,case_id,points) VALUES (?,?,?,?)",
                    (guild.id, user.id, case_id, points),
                )
        return case_id

    async def moderation_result(self, interaction, action, member, reason, case_id):
        embed = brand_embed(
            f"{action} • Case #{case_id}",
            f"**User:** {member.mention} (`{member.id}`)\n**Reason:** {reason or 'No reason provided'}",
            color=SUCCESS_COLOR,
        )
        await self.reply(interaction, embed)
        await self.send_log(interaction.guild, f"{action} • Case #{case_id}", embed.description, discord.Color(BRAND_COLOR))

    @app_commands.command(name="warn", description="Warn a user")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", points: app_commands.Range[int, 1, 10] = 1):
        if not await self.require_admin(interaction): return
        case_id = self.add_case(interaction.guild, member, interaction.user, "Warning", reason, points)
        try:
            await member.send(embed=brand_embed(f"Warning in {interaction.guild.name}", f"**Reason:** {reason}\n**Points:** {points}", color=ERROR_COLOR))
        except discord.HTTPException:
            pass
        await self.moderation_result(interaction, "Warning", member, reason, case_id)
        threshold = int(self.setting(interaction.guild, "warn_threshold", "3") or 3)
        with self.connect_db() as db:
            total = db.execute("SELECT COALESCE(SUM(points),0) total FROM warning_points WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id)).fetchone()["total"]
        if total >= threshold:
            try:
                await member.timeout(discord.utils.utcnow() + timedelta(hours=1), reason=f"Warning threshold reached ({total})")
            except discord.HTTPException:
                pass

    @app_commands.command(name="kick", description="Kick a user")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await self.require_admin(interaction): return
        case_id = self.add_case(interaction.guild, member, interaction.user, "Kick", reason)
        await member.kick(reason=reason)
        await self.moderation_result(interaction, "Kick", member, reason, case_id)

    @app_commands.command(name="ban", description="Ban a user")
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not await self.require_admin(interaction): return
        case_id = self.add_case(interaction.guild, member, interaction.user, "Ban", reason)
        await member.ban(reason=reason)
        await self.moderation_result(interaction, "Ban", member, reason, case_id)

    @app_commands.command(name="removeban", description="Unban a user by Discord user ID")
    async def removeban(self, interaction: discord.Interaction, user_id: str, reason: str = "Appeal accepted"):
        if not await self.require_admin(interaction): return
        if not user_id.isdigit():
            return await self.reply(interaction, brand_embed("Invalid User ID", "Enter the numeric Discord user ID.", color=ERROR_COLOR))
        user = await self.bot.fetch_user(int(user_id))
        await interaction.guild.unban(user, reason=reason)
        case_id = self.add_case(interaction.guild, user, interaction.user, "Unban", reason)
        await self.moderation_result(interaction, "Unban", user, reason, case_id)

    @app_commands.command(name="timeout", description="Timeout a user")
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason provided"):
        if not await self.require_admin(interaction): return
        seconds = parse_duration(duration)
        if not seconds or seconds > 2419200:
            return await self.reply(interaction, brand_embed("Invalid Duration", "Use `10m`, `2h`, or `7d` (maximum 28 days).", color=ERROR_COLOR))
        await member.timeout(discord.utils.utcnow() + timedelta(seconds=seconds), reason=reason)
        case_id = self.add_case(interaction.guild, member, interaction.user, "Timeout", f"{reason} • {duration}")
        await self.moderation_result(interaction, "Timeout", member, f"{reason}\n**Duration:** {duration}", case_id)

    @app_commands.command(name="removetimeout", description="Remove a timeout")
    async def removetimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Timeout removed"):
        if not await self.require_admin(interaction): return
        await member.timeout(None, reason=reason)
        case_id = self.add_case(interaction.guild, member, interaction.user, "Timeout Removed", reason)
        await self.moderation_result(interaction, "Timeout Removed", member, reason, case_id)

    @app_commands.command(name="cleanchat", description="Delete messages")
    async def cleanchat(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 200]):
        if not await self.require_admin(interaction): return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(embed=brand_embed("Chat Cleaned", f"Deleted **{len(deleted)}** messages.", color=SUCCESS_COLOR), ephemeral=True)

    @app_commands.command(name="warnings", description="Check a user's warnings")
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.require_admin(interaction): return
        with self.connect_db() as db:
            rows = db.execute("SELECT c.id,c.reason,c.created_at,w.points FROM moderation_cases c JOIN warning_points w ON w.case_id=c.id WHERE c.guild_id=? AND c.user_id=? ORDER BY c.id DESC LIMIT 15", (interaction.guild.id, member.id)).fetchall()
        text = "\n".join(f"`#{r['id']}` • **{r['points']} point(s)** • {r['reason']}" for r in rows) or "No warnings found."
        await self.reply(interaction, brand_embed(f"Warnings • {member}", text))

    @app_commands.command(name="clearwarnings", description="Clear a user's warnings")
    async def clearwarnings(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.require_admin(interaction): return
        with self.connect_db() as db:
            db.execute("DELETE FROM warning_points WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        await self.reply(interaction, brand_embed("Warnings Cleared", f"All warning points for {member.mention} were cleared.", color=SUCCESS_COLOR))

    @app_commands.command(name="lockdown", description="Lock a channel")
    async def lockdown(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await self.require_admin(interaction): return
        channel = channel or interaction.channel
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Locked by {interaction.user}")
        await self.reply(interaction, brand_embed("Channel Locked", f"{channel.mention} is now locked.", color=ERROR_COLOR))

    @app_commands.command(name="unlock", description="Unlock a channel")
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await self.require_admin(interaction): return
        channel = channel or interaction.channel
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
        await self.reply(interaction, brand_embed("Channel Unlocked", f"{channel.mention} is now unlocked.", color=SUCCESS_COLOR))

    @app_commands.command(name="servercase", description="View server moderation cases")
    async def servercase(self, interaction: discord.Interaction, case_id: int | None = None):
        if not await self.require_admin(interaction): return
        with self.connect_db() as db:
            if case_id:
                rows = db.execute("SELECT * FROM moderation_cases WHERE guild_id=? AND id=?", (interaction.guild.id, case_id)).fetchall()
            else:
                rows = db.execute("SELECT * FROM moderation_cases WHERE guild_id=? ORDER BY id DESC LIMIT 15", (interaction.guild.id,)).fetchall()
        text = "\n".join(f"`#{r['id']}` **{r['action']}** • <@{r['user_id']}> • {r['reason']}" for r in rows) or "No cases found."
        await self.reply(interaction, brand_embed("Server Cases", text))

    @app_commands.command(name="userinfo", description="Get user information")
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        roles = [role.mention for role in member.roles[1:]][-10:]
        embed = brand_embed(f"User Information • {member}")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown", inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        embed.add_field(name="Roles", value=" ".join(roles) or "None", inline=False)
        await self.reply(interaction, embed)

    @app_commands.command(name="automod_setup", description="Configure auto-moderation")
    async def automod_setup(self, interaction: discord.Interaction, enabled: bool, block_invites: bool = True, block_spam: bool = True):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "automod_enabled", int(enabled))
        self.save_setting(interaction.guild, "automod_invites", int(block_invites))
        self.save_setting(interaction.guild, "automod_spam", int(block_spam))
        await self.reply(interaction, brand_embed("Auto-Moderation Updated", f"**Enabled:** {enabled}\n**Block invites:** {block_invites}\n**Block spam:** {block_spam}", color=SUCCESS_COLOR))

    @app_commands.command(name="warnthreshold", description="Configure warning thresholds")
    async def warnthreshold(self, interaction: discord.Interaction, points: app_commands.Range[int, 1, 20]):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "warn_threshold", points)
        await self.reply(interaction, brand_embed("Warning Threshold", f"Members will receive a one-hour timeout at **{points} warning points**.", color=SUCCESS_COLOR))

    @app_commands.command(name="ghostping", description="Configure ghost-ping detection")
    async def ghostping(self, interaction: discord.Interaction, enabled: bool, log_channel: discord.TextChannel | None = None):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "ghostping_enabled", int(enabled))
        if log_channel: self.save_setting(interaction.guild, "ghostping_channel", log_channel.id)
        await self.reply(interaction, brand_embed("Ghost-Ping Detection", f"**Enabled:** {enabled}\n**Log channel:** {log_channel.mention if log_channel else 'Use bot logs'}", color=SUCCESS_COLOR))

    @app_commands.command(name="welcomesetup", description="Configure welcome messages")
    async def welcomesetup(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Welcome {user} to **{server}**!"):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "welcome_channel", channel.id)
        self.save_setting(interaction.guild, "welcome_message", message)
        await self.reply(interaction, brand_embed("Welcome Messages Configured", f"Messages will be posted in {channel.mention}.\n\n{message}", color=SUCCESS_COLOR))

    @app_commands.command(name="farewellsetup", description="Configure farewell messages")
    async def farewellsetup(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Goodbye {user} — thanks for being part of **{server}**."):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "farewell_channel", channel.id)
        self.save_setting(interaction.guild, "farewell_message", message)
        await self.reply(interaction, brand_embed("Farewell Messages Configured", f"Messages will be posted in {channel.mention}.\n\n{message}", color=SUCCESS_COLOR))

    @app_commands.command(name="leveling", description="Configure the leveling system")
    async def leveling(self, interaction: discord.Interaction, enabled: bool, announcement_channel: discord.TextChannel | None = None):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "leveling_enabled", int(enabled))
        if announcement_channel: self.save_setting(interaction.guild, "level_channel", announcement_channel.id)
        await self.reply(interaction, brand_embed("Leveling Updated", f"**Enabled:** {enabled}\n**Announcements:** {announcement_channel.mention if announcement_channel else 'Current channel'}", color=SUCCESS_COLOR))

    @app_commands.command(name="rank", description="View your or another user's rank")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        with self.connect_db() as db:
            row = db.execute("SELECT xp,messages FROM member_stats WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id)).fetchone()
            xp, messages = (row["xp"], row["messages"]) if row else (0, 0)
            position = db.execute("SELECT COUNT(*)+1 rank FROM member_stats WHERE guild_id=? AND xp>?", (interaction.guild.id, xp)).fetchone()["rank"]
        level = level_from_xp(xp)
        next_xp = (level + 1) ** 2 * 100
        embed = brand_embed(f"Rank • {member.display_name}", f"**Level:** {level}\n**XP:** {xp:,} / {next_xp:,}\n**Messages:** {messages:,}\n**Server rank:** #{position}")
        embed.set_thumbnail(url=member.display_avatar.url)
        await self.reply(interaction, embed)

    @app_commands.command(name="leaderboard", description="View the XP leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        with self.connect_db() as db:
            rows = db.execute("SELECT user_id,xp FROM member_stats WHERE guild_id=? ORDER BY xp DESC LIMIT 10", (interaction.guild.id,)).fetchall()
        text = "\n".join(f"**{i}.** <@{r['user_id']}> — Level **{level_from_xp(r['xp'])}** • {r['xp']:,} XP" for i, r in enumerate(rows, 1)) or "No XP has been earned yet."
        await self.reply(interaction, brand_embed("⭐ XP Leaderboard", text))

    @app_commands.command(name="setxp", description="Set a user's XP")
    async def setxp(self, interaction: discord.Interaction, member: discord.Member, xp: app_commands.Range[int, 0, 100000000]):
        if not await self.require_admin(interaction): return
        with self.connect_db() as db:
            db.execute("INSERT INTO member_stats(guild_id,user_id,xp) VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=excluded.xp", (interaction.guild.id, member.id, xp))
        await self.reply(interaction, brand_embed("XP Updated", f"{member.mention} now has **{xp:,} XP**.", color=SUCCESS_COLOR))

    @app_commands.command(name="resetxp", description="Reset a user's XP")
    async def resetxp(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.require_admin(interaction): return
        with self.connect_db() as db:
            db.execute("INSERT INTO member_stats(guild_id,user_id,xp) VALUES(?,?,0) ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=0", (interaction.guild.id, member.id))
        await self.reply(interaction, brand_embed("XP Reset", f"Reset XP for {member.mention}.", color=SUCCESS_COLOR))

    @app_commands.command(name="levelroles", description="Manage level role rewards")
    async def levelroles(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 1000], role: discord.Role | None = None, remove: bool = False):
        if not await self.require_admin(interaction): return
        with self.connect_db() as db:
            if remove:
                db.execute("DELETE FROM level_rewards WHERE guild_id=? AND level=?", (interaction.guild.id, level))
            elif role:
                db.execute("INSERT OR REPLACE INTO level_rewards(guild_id,level,role_id) VALUES(?,?,?)", (interaction.guild.id, level, role.id))
            else:
                return await self.reply(interaction, brand_embed("Role Required", "Choose a role, or set `remove` to true.", color=ERROR_COLOR))
        await self.reply(interaction, brand_embed("Level Reward Updated", f"Level **{level}** reward: {'removed' if remove else role.mention}", color=SUCCESS_COLOR))

    @app_commands.command(name="messages", description="Check a user's message count")
    async def messages(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        with self.connect_db() as db:
            row = db.execute("SELECT messages FROM member_stats WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id)).fetchone()
        await self.reply(interaction, brand_embed("💬 Message Count", f"{member.mention} has sent **{row['messages'] if row else 0:,} tracked messages**."))

    @app_commands.command(name="messageleaderboard", description="View the message leaderboard")
    async def messageleaderboard(self, interaction: discord.Interaction):
        with self.connect_db() as db:
            rows = db.execute("SELECT user_id,messages FROM member_stats WHERE guild_id=? ORDER BY messages DESC LIMIT 10", (interaction.guild.id,)).fetchall()
        text = "\n".join(f"**{i}.** <@{r['user_id']}> — {r['messages']:,}" for i, r in enumerate(rows, 1)) or "No messages tracked yet."
        await self.reply(interaction, brand_embed("💬 Message Leaderboard", text))

    @app_commands.command(name="message_invite_toggle", description="Enable or disable message and invite counting")
    async def message_invite_toggle(self, interaction: discord.Interaction, enabled: bool):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "tracking_enabled", int(enabled))
        await self.reply(interaction, brand_embed("Tracking Updated", f"Message and invite tracking is **{'enabled' if enabled else 'disabled'}**.", color=SUCCESS_COLOR))

    @app_commands.command(name="invites", description="Check a user's invites")
    async def invites(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        with self.connect_db() as db:
            row = db.execute("SELECT invites,bonus_invites FROM member_stats WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id)).fetchone()
        regular, bonus = (row["invites"], row["bonus_invites"]) if row else (0, 0)
        await self.reply(interaction, brand_embed("✉️ Invite Information", f"{member.mention}\n**Regular:** {regular}\n**Bonus:** {bonus}\n**Total:** {regular + bonus}"))

    @app_commands.command(name="inviteleaderboard", description="View the invite leaderboard")
    async def inviteleaderboard(self, interaction: discord.Interaction):
        with self.connect_db() as db:
            rows = db.execute("SELECT user_id,invites+bonus_invites total FROM member_stats WHERE guild_id=? ORDER BY total DESC LIMIT 10", (interaction.guild.id,)).fetchall()
        text = "\n".join(f"**{i}.** <@{r['user_id']}> — {r['total']}" for i, r in enumerate(rows, 1)) or "No invites tracked yet."
        await self.reply(interaction, brand_embed("✉️ Invite Leaderboard", text))

    async def change_invites(self, interaction, member, amount, bonus=False):
        if not await self.require_admin(interaction): return
        column = "bonus_invites" if bonus else "invites"
        with self.connect_db() as db:
            db.execute("INSERT OR IGNORE INTO member_stats(guild_id,user_id) VALUES(?,?)", (interaction.guild.id, member.id))
            db.execute(f"UPDATE member_stats SET {column}=MAX(0,{column}+?) WHERE guild_id=? AND user_id=?", (amount, interaction.guild.id, member.id))
        await self.reply(interaction, brand_embed("Invites Updated", f"Changed {member.mention}'s **{column.replace('_',' ')}** by **{amount:+}**.", color=SUCCESS_COLOR))

    @app_commands.command(name="addinvite", description="Add invites to a user")
    async def addinvite(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1000]):
        await self.change_invites(interaction, member, amount)

    @app_commands.command(name="removeinvite", description="Remove invites from a user")
    async def removeinvite(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1000]):
        await self.change_invites(interaction, member, -amount)

    @app_commands.command(name="addbonus", description="Add bonus invites to a user")
    async def addbonus(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1000]):
        await self.change_invites(interaction, member, amount, True)

    @app_commands.command(name="resetinvites", description="Reset a user's invites")
    async def resetinvites(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.require_admin(interaction): return
        with self.connect_db() as db:
            db.execute("UPDATE member_stats SET invites=0,bonus_invites=0 WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
        await self.reply(interaction, brand_embed("Invites Reset", f"Reset invite data for {member.mention}.", color=SUCCESS_COLOR))

    @app_commands.command(name="ignoredrole", description="Add, remove or list ignored roles")
    async def ignoredrole(self, interaction: discord.Interaction, module: str, action: str, role: discord.Role | None = None):
        if not await self.require_admin(interaction): return
        module, action = module.lower(), action.lower()
        if module not in ("automod", "leveling") or action not in ("add", "remove", "list"):
            return await self.reply(interaction, brand_embed("Invalid Option", "Module: `automod` or `leveling`. Action: `add`, `remove`, or `list`.", color=ERROR_COLOR))
        with self.connect_db() as db:
            if action == "add" and role: db.execute("INSERT OR IGNORE INTO ignored_items VALUES(?,?,?,?)", (interaction.guild.id,module,"role",role.id))
            elif action == "remove" and role: db.execute("DELETE FROM ignored_items WHERE guild_id=? AND module=? AND item_type='role' AND item_id=?", (interaction.guild.id,module,role.id))
            rows = db.execute("SELECT item_id FROM ignored_items WHERE guild_id=? AND module=? AND item_type='role'", (interaction.guild.id,module)).fetchall()
        await self.reply(interaction, brand_embed("Ignored Roles", " ".join(f"<@&{r['item_id']}>" for r in rows) or "None"))

    @app_commands.command(name="ignoredchannel", description="Add, remove or list ignored channels")
    async def ignoredchannel(self, interaction: discord.Interaction, module: str, action: str, channel: discord.TextChannel | None = None):
        if not await self.require_admin(interaction): return
        module, action = module.lower(), action.lower()
        if module not in ("automod", "leveling") or action not in ("add", "remove", "list"):
            return await self.reply(interaction, brand_embed("Invalid Option", "Module: `automod` or `leveling`. Action: `add`, `remove`, or `list`.", color=ERROR_COLOR))
        with self.connect_db() as db:
            if action == "add" and channel: db.execute("INSERT OR IGNORE INTO ignored_items VALUES(?,?,?,?)", (interaction.guild.id,module,"channel",channel.id))
            elif action == "remove" and channel: db.execute("DELETE FROM ignored_items WHERE guild_id=? AND module=? AND item_type='channel' AND item_id=?", (interaction.guild.id,module,channel.id))
            rows = db.execute("SELECT item_id FROM ignored_items WHERE guild_id=? AND module=? AND item_type='channel'", (interaction.guild.id,module)).fetchall()
        await self.reply(interaction, brand_embed("Ignored Channels", " ".join(f"<#{r['item_id']}>" for r in rows) or "None"))

    @app_commands.command(name="giveawaytoggle", description="Enable or disable giveaways")
    async def giveawaytoggle(self, interaction: discord.Interaction, enabled: bool):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "giveaways_enabled", int(enabled))
        await self.reply(interaction, brand_embed("Giveaways Updated", f"Giveaways are **{'enabled' if enabled else 'disabled'}**.", color=SUCCESS_COLOR))

    @app_commands.command(name="gstart", description="Start a giveaway")
    async def gstart(self, interaction: discord.Interaction, duration: str, winners: app_commands.Range[int, 1, 20], prize: str):
        if not await self.require_admin(interaction): return
        if self.setting(interaction.guild, "giveaways_enabled", "1") != "1":
            return await self.reply(interaction, brand_embed("Giveaways Disabled", "Enable them with `/giveawaytoggle`.", color=ERROR_COLOR))
        seconds = parse_duration(duration)
        if not seconds:
            return await self.reply(interaction, brand_embed("Invalid Duration", "Use `30m`, `2h`, or `1d`.", color=ERROR_COLOR))
        ends_at = int(time.time()) + seconds
        embed = brand_embed("🎉 Giveaway", f"**Prize:** {prize}\n**Winners:** {winners}\n**Ends:** <t:{ends_at}:R>\n\nReact with 🎉 to enter!")
        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()
        await message.add_reaction("🎉")
        with self.connect_db() as db:
            db.execute("INSERT INTO giveaways VALUES(?,?,?,?,?,?,0)", (message.id,interaction.guild.id,interaction.channel.id,prize,winners,ends_at))
        asyncio.create_task(self.finish_giveaway(message.id, seconds))

    async def finish_giveaway(self, message_id, delay=0):
        if delay: await asyncio.sleep(delay)
        with self.connect_db() as db:
            row = db.execute("SELECT * FROM giveaways WHERE message_id=? AND ended=0", (message_id,)).fetchone()
        if not row: return
        channel = self.bot.get_channel(row["channel_id"])
        if not channel: return
        try:
            message = await channel.fetch_message(message_id)
            reaction = discord.utils.get(message.reactions, emoji="🎉")
            users = [u async for u in reaction.users()] if reaction else []
            users = [u for u in users if not u.bot]
            winners = random.sample(users, min(row["winners"], len(users))) if users else []
            result = " ".join(u.mention for u in winners) if winners else "No valid entries"
            await channel.send(embed=brand_embed("🎉 Giveaway Ended", f"**Prize:** {row['prize']}\n**Winner(s):** {result}", color=SUCCESS_COLOR))
            with self.connect_db() as db: db.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
        except discord.HTTPException:
            pass

    @app_commands.command(name="g-end", description="End a giveaway early")
    async def g_end(self, interaction: discord.Interaction, message_id: str):
        if not await self.require_admin(interaction): return
        if not message_id.isdigit(): return await self.reply(interaction, "Enter a valid message ID.")
        await interaction.response.defer(ephemeral=True)
        await self.finish_giveaway(int(message_id))
        await interaction.followup.send(embed=brand_embed("Giveaway Ended", "The giveaway was ended.", color=SUCCESS_COLOR), ephemeral=True)

    @app_commands.command(name="g-reroll", description="Reroll a giveaway winner")
    async def g_reroll(self, interaction: discord.Interaction, message_id: str):
        if not await self.require_admin(interaction): return
        try:
            message = await interaction.channel.fetch_message(int(message_id))
            reaction = discord.utils.get(message.reactions, emoji="🎉")
            users = [u async for u in reaction.users() if not u.bot] if reaction else []
            winner = random.choice(users) if users else None
            await self.reply(interaction, brand_embed("🎉 Giveaway Rerolled", winner.mention if winner else "No valid entries.", color=SUCCESS_COLOR))
        except (ValueError, discord.HTTPException):
            await self.reply(interaction, brand_embed("Giveaway Not Found", "Check the message ID and channel.", color=ERROR_COLOR))

    @app_commands.command(name="setup_starboard", description="Configure the starboard")
    async def setup_starboard(self, interaction: discord.Interaction, channel: discord.TextChannel, emoji: str = "⭐", threshold: app_commands.Range[int, 1, 100] = 3, prevent_self_star: bool = True):
        if not await self.require_admin(interaction): return
        self.save_setting(interaction.guild, "starboard_channel", channel.id)
        self.save_setting(interaction.guild, "starboard_emoji", emoji)
        self.save_setting(interaction.guild, "starboard_threshold", threshold)
        self.save_setting(interaction.guild, "starboard_no_self", int(prevent_self_star))
        await self.reply(interaction, brand_embed("⭐ Starboard Configured", f"**Channel:** {channel.mention}\n**Emoji:** {emoji}\n**Threshold:** {threshold}", color=SUCCESS_COLOR))

    @app_commands.command(name="ticketsetup", description="Configure the ticket system")
    async def ticketsetup(self, interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role, panel_channel: discord.TextChannel):
        if not await self.require_admin(interaction): return
        self.set_setting(f"guild:{interaction.guild.id}:ticket_category_id", category.id)
        self.set_setting(f"guild:{interaction.guild.id}:ticket_staff_role_id", staff_role.id)
        await self.reply(interaction, brand_embed("🎫 Ticket System Configured", f"**Category:** {category.name}\n**Staff:** {staff_role.mention}\n**Panel:** {panel_channel.mention}\n\nUse `/addticketbutton`, then `/ticketpanel` in the panel channel.", color=SUCCESS_COLOR))

    @app_commands.command(name="applicationsetup", description="Configure the application system")
    async def applicationsetup(self, interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role, panel_channel: discord.TextChannel, application_name: str = "Manager Application"):
        if not await self.require_admin(interaction): return
        with self.connect_db() as db:
            db.execute("INSERT INTO ticket_types(label,emoji,style,questions,ping_role_ids) VALUES(?,?,?,?,?)", (application_name,"📝","primary",json.dumps(["Why are you applying?","What experience do you have?"]),json.dumps([staff_role.id])))
        self.set_setting(f"guild:{interaction.guild.id}:ticket_category_id", category.id)
        self.set_setting(f"guild:{interaction.guild.id}:ticket_staff_role_id", staff_role.id)
        await self.reply(interaction, brand_embed("📝 Application System Configured", f"Added **{application_name}** as a private ticket application. Use `/ticketpanel` in {panel_channel.mention} to post it.", color=SUCCESS_COLOR))

    @app_commands.command(name="help", description="Bot help and links")
    async def help_command(self, interaction: discord.Interaction):
        await self.reply(interaction, brand_embed("🔗 APL Bot Help", "Use `/commands` for every command. Administrators can use `/dashboard` to configure this server.\n\nIf a command is missing, restart the Railway deployment and wait up to one hour for Discord's global command cache."))

    @app_commands.command(name="website", description="Open the bot dashboard")
    async def website(self, interaction: discord.Interaction):
        command = self.bot.tree.get_command("dashboard")
        if command:
            await command.callback(interaction)
        else:
            await self.reply(interaction, brand_embed("Dashboard Unavailable", "The dashboard URL is not configured.", color=ERROR_COLOR))

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot:
            return
        enabled = self.setting(message.guild, "tracking_enabled", "1") == "1"
        leveling = self.setting(message.guild, "leveling_enabled", "0") == "1"
        if enabled or leveling:
            now = time.monotonic()
            key = (message.guild.id, message.author.id)
            gain = random.randint(15, 25) if leveling and now - self.message_cooldowns.get(key, 0) >= 60 else 0
            if gain: self.message_cooldowns[key] = now
            with self.connect_db() as db:
                db.execute("INSERT OR IGNORE INTO member_stats(guild_id,user_id) VALUES(?,?)", key)
                before = db.execute("SELECT xp FROM member_stats WHERE guild_id=? AND user_id=?", key).fetchone()["xp"]
                db.execute("UPDATE member_stats SET messages=messages+?,xp=xp+? WHERE guild_id=? AND user_id=?", (1 if enabled else 0,gain,*key))
            if gain and level_from_xp(before + gain) > level_from_xp(before):
                level = level_from_xp(before + gain)
                with self.connect_db() as db:
                    reward = db.execute("SELECT role_id FROM level_rewards WHERE guild_id=? AND level=?", (message.guild.id,level)).fetchone()
                if reward:
                    role = message.guild.get_role(reward["role_id"])
                    if role:
                        try: await message.author.add_roles(role, reason=f"Reached level {level}")
                        except discord.HTTPException: pass
                channel = message.guild.get_channel(int(self.setting(message.guild,"level_channel",str(message.channel.id)) or message.channel.id))
                if channel: await channel.send(embed=brand_embed("⭐ Level Up!", f"{message.author.mention} reached **Level {level}**!", color=SUCCESS_COLOR))
        if self.setting(message.guild, "automod_enabled", "0") == "1" and not message.author.guild_permissions.manage_messages:
            if self.setting(message.guild,"automod_invites","1") == "1" and re.search(r"discord(?:\.gg|\.com/invite)/[\w-]+", message.content, re.I):
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention}, Discord invite links are not allowed.", delete_after=5)
                except discord.HTTPException: pass
                return
            if self.setting(message.guild,"automod_spam","1") == "1":
                spam_key = (message.guild.id, message.author.id)
                recent = [stamp for stamp in self.spam_cache.get(spam_key, []) if time.monotonic() - stamp < 6]
                recent.append(time.monotonic())
                self.spam_cache[spam_key] = recent
                if len(recent) >= 6:
                    try:
                        await message.delete()
                        await message.author.timeout(discord.utils.utcnow() + timedelta(minutes=5), reason="APL Auto-Mod spam detection")
                        await message.channel.send(embed=brand_embed("Auto-Mod • Spam", f"{message.author.mention} was timed out for five minutes.", color=ERROR_COLOR), delete_after=10)
                    except discord.HTTPException:
                        pass

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            try:
                self.invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in await guild.invites()}
            except discord.HTTPException:
                self.invite_cache[guild.id] = {}

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if not message.guild or message.author.bot or not message.mentions:
            return
        if self.setting(message.guild, "ghostping_enabled", "0") != "1":
            return
        channel_id = int(self.setting(message.guild, "ghostping_channel", "0") or 0)
        channel = message.guild.get_channel(channel_id)
        description = f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Mentioned:** {' '.join(m.mention for m in message.mentions)}\n**Message:** {message.content[:1000] or '*No text*'}"
        if channel:
            await channel.send(embed=brand_embed("Ghost Ping Detected", description, color=ERROR_COLOR))
        else:
            await self.send_log(message.guild, "Ghost Ping Detected", description, discord.Color(ERROR_COLOR))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.bot.user.id or not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if not guild or str(payload.emoji) != self.setting(guild, "starboard_emoji", "⭐"):
            return
        starboard = guild.get_channel(int(self.setting(guild, "starboard_channel", "0") or 0))
        source_channel = guild.get_channel(payload.channel_id)
        if not starboard or not source_channel or starboard.id == source_channel.id:
            return
        try:
            message = await source_channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        reaction = discord.utils.get(message.reactions, emoji=payload.emoji)
        threshold = int(self.setting(guild, "starboard_threshold", "3") or 3)
        if not reaction or reaction.count < threshold:
            return
        if self.setting(guild, "starboard_no_self", "1") == "1" and payload.user_id == message.author.id:
            return
        embed = brand_embed(f"⭐ {reaction.count} • #{source_channel.name}", message.content[:3500] or "*Attachment or embed*")
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        if message.attachments and message.attachments[0].content_type and message.attachments[0].content_type.startswith("image/"):
            embed.set_image(url=message.attachments[0].url)
        embed.add_field(name="Original", value=f"[Jump to message]({message.jump_url})", inline=False)
        with self.connect_db() as db:
            existing = db.execute("SELECT starboard_message_id FROM starboard_posts WHERE source_message_id=?", (message.id,)).fetchone()
        if existing:
            try:
                posted = await starboard.fetch_message(existing["starboard_message_id"])
                await posted.edit(embed=embed)
            except discord.HTTPException:
                pass
        else:
            posted = await starboard.send(embed=embed)
            with self.connect_db() as db:
                db.execute("INSERT INTO starboard_posts VALUES(?,?,?)", (message.id,posted.id,guild.id))

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if self.setting(member.guild, "tracking_enabled", "1") == "1":
            try:
                latest = {invite.code: invite.uses or 0 for invite in await member.guild.invites()}
                previous = self.invite_cache.get(member.guild.id, {})
                used = next((invite for invite in await member.guild.invites() if (invite.uses or 0) > previous.get(invite.code, 0)), None)
                self.invite_cache[member.guild.id] = latest
                if used and used.inviter:
                    with self.connect_db() as db:
                        db.execute("INSERT OR IGNORE INTO member_stats(guild_id,user_id) VALUES(?,?)", (member.guild.id,used.inviter.id))
                        db.execute("UPDATE member_stats SET invites=invites+1 WHERE guild_id=? AND user_id=?", (member.guild.id,used.inviter.id))
            except discord.HTTPException:
                pass
        channel = member.guild.get_channel(int(self.setting(member.guild,"welcome_channel","0") or 0))
        if channel:
            text = self.setting(member.guild,"welcome_message","Welcome {user} to **{server}**!").replace("{user}",member.mention).replace("{server}",member.guild.name).replace("{member_count}",str(member.guild.member_count))
            embed = brand_embed("👋 Welcome!", text, color=SUCCESS_COLOR); embed.set_thumbnail(url=member.display_avatar.url)
            await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        channel = member.guild.get_channel(int(self.setting(member.guild,"farewell_channel","0") or 0))
        if channel:
            text = self.setting(member.guild,"farewell_message","Goodbye {user} — thanks for being part of **{server}**.").replace("{user}",str(member)).replace("{server}",member.guild.name).replace("{member_count}",str(member.guild.member_count))
            await channel.send(embed=brand_embed("👋 Member Left", text, color=ERROR_COLOR))


async def setup_byronic(bot, **helpers):
    cog = ByronicFeatures(bot, **helpers)
    await bot.add_cog(cog)
    with cog.connect_db() as db:
        pending = db.execute("SELECT message_id,ends_at FROM giveaways WHERE ended=0").fetchall()
    for row in pending:
        asyncio.create_task(cog.finish_giveaway(row["message_id"], max(0, row["ends_at"] - int(time.time()))))
    return cog
