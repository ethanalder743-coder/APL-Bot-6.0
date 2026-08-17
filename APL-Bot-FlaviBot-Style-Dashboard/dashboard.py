import html
import os
import secrets
import time

import discord
from aiohttp import web


def dashboard_base_url():
    configured = os.getenv("DASHBOARD_URL", "").strip().rstrip("/")
    if configured:
        return configured
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{railway_domain}" if railway_domain else ""


def option_list(items, selected, label):
    options = [f'<option value="0">Not configured</option>']
    for item in items:
        is_selected = " selected" if str(item.id) == str(selected) else ""
        options.append(f'<option value="{item.id}"{is_selected}>{html.escape(label(item))}</option>')
    return "".join(options)


def page(guild, token, get_value, saved=False):
    text_channels = sorted(guild.text_channels, key=lambda c: (c.position, c.name))
    categories = sorted(guild.categories, key=lambda c: (c.position, c.name))
    roles = [r for r in reversed(guild.roles) if not r.is_default() and not r.managed]
    channels = lambda key: option_list(text_channels, get_value(key, "0"), lambda c: f"#{c.name}")
    cats = lambda key: option_list(categories, get_value(key, "0"), lambda c: c.name)
    role_options = lambda key: option_list(roles, get_value(key, "0"), lambda r: f"@{r.name}")
    checked = lambda key, default="0": " checked" if get_value(key, default) == "1" else ""
    guild_icon = guild.icon.url if getattr(guild, "icon", None) else ""
    icon_markup = f'<img src="{html.escape(guild_icon)}" alt="">' if guild_icon else "APL"
    saved_banner = '<div class="saved">✓ Settings saved. The bot is using the new configuration.</div>' if saved else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>APL Bot Control • {html.escape(guild.name)}</title>
<style>
:root{{--bg:#11121a;--sidebar:#171821;--panel:#1c1d28;--panel2:#222431;--line:#2f3140;--text:#f7f7fb;--muted:#9096aa;--purple:#8b5cf6;--purple2:#a78bfa;--green:#27c56f;--red:#ed4245}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;min-height:100vh}}
.shell{{display:grid;grid-template-columns:274px minmax(0,1fr);min-height:100vh}} aside{{border-right:1px solid var(--line);background:var(--sidebar);position:sticky;top:0;height:100vh;overflow-y:auto;scrollbar-width:thin;scrollbar-color:#555869 transparent}}
.brand{{height:62px;display:flex;gap:11px;align-items:center;padding:0 18px;border-bottom:1px solid var(--line)}} .brand-mark{{width:31px;height:31px;border:1px solid #4b4e5d;border-radius:8px;display:grid;place-items:center;color:var(--purple2);font-weight:1000}} .brand b{{display:block;font-size:22px;letter-spacing:-.5px}} .brand small{{color:var(--muted)}}
.server-card{{display:flex;align-items:center;gap:10px;margin:14px 11px 17px;padding:10px;background:#11121a;border:1px solid var(--line);border-radius:10px}} .server-icon,.bot-icon{{flex:0 0 auto;width:34px;height:34px;border-radius:50%;background:linear-gradient(145deg,#6d45df,#9f7aea);display:grid;place-items:center;font-size:10px;font-weight:900;overflow:hidden}} .server-icon img{{width:100%;height:100%;object-fit:cover}} .server-card strong{{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}} .server-card span{{font-size:11px;color:var(--green)}}
.nav-group{{padding:0 11px 13px}} .nav-label{{display:block;color:#6f86b8;font-size:10px;letter-spacing:.08em;margin:13px 10px 7px;text-transform:uppercase}} nav button{{display:flex;align-items:center;gap:10px;width:100%;border:0;background:transparent;color:#93a4c9;padding:9px 11px;margin:1px 0;border-radius:7px;cursor:pointer;text-align:left;font-weight:600}} nav button .nav-icon{{width:19px;text-align:center;color:#8292b4}} nav button:hover{{background:#20222e;color:white}} nav button.active{{background:linear-gradient(90deg,rgba(139,92,246,.2),rgba(139,92,246,.05));color:var(--purple2)}} nav button.active .nav-icon{{color:var(--purple2)}}
.content{{min-width:0}} .topbar{{height:62px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 28px;background:rgba(23,24,33,.82);position:sticky;top:0;z-index:20;backdrop-filter:blur(14px)}} .crumbs{{display:flex;align-items:center;gap:10px;color:#8fa4ce}} .crumbs b{{color:var(--purple2)}} .top-status{{display:flex;align-items:center;gap:9px;color:#cbd0dc}} .online-dot{{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 10px var(--green)}}
main{{padding:24px 28px 50px;max-width:1500px;width:100%;margin:0 auto}} .bot-row{{display:grid;grid-template-columns:repeat(5,minmax(145px,1fr));gap:10px;margin-bottom:22px;overflow-x:auto}} .bot-choice{{min-width:145px;display:flex;align-items:center;gap:9px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px;color:var(--muted)}} .bot-choice.active{{border:2px solid var(--purple);padding:9px 11px;color:white;box-shadow:0 0 0 2px rgba(139,92,246,.12)}} .bot-choice .bot-icon{{width:31px;height:31px}} .bot-choice small{{margin-left:auto;color:var(--green);font-weight:900}}
.notice{{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:16px;margin-bottom:28px}} .notice .bot-icon{{width:40px;height:40px;font-size:16px}} .notice b{{display:block;margin-bottom:3px}} .notice span{{color:#8e9fc5;font-size:12px}}
.page-title{{display:flex;justify-content:space-between;align-items:end;margin-bottom:20px}} h1{{margin:0;font-size:27px}} .page-title p{{color:var(--muted);margin:6px 0 0}} .status{{padding:7px 11px;background:#153427;color:#65dc97;border-radius:99px;font-size:12px;font-weight:800}}
.tab{{display:none}} .tab.active{{display:block}} .grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px}} .card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;min-height:290px}} .card-head{{display:flex;gap:13px;padding:20px 22px;border-bottom:1px solid var(--line)}} .card-head .feature-icon{{font-size:19px;color:var(--purple2);width:26px}} .card-body{{padding:16px 22px 22px}} .wide{{grid-column:1/-1}}
h2{{font-size:16px;margin:0 0 7px}} .hint{{color:var(--muted);margin:0;line-height:1.55}} label{{display:block;color:#eef0f5;font-size:12px;font-weight:800;margin:14px 0 7px}} select,input[type=text],input[type=number],textarea{{width:100%;background:#171822;color:white;border:1px solid #383a4b;border-radius:9px;padding:11px 12px;outline:0}} textarea{{min-height:90px;resize:vertical}} select:focus,input:focus,textarea:focus{{border-color:var(--purple)}}
.toggle{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:11px 0;border-bottom:1px solid rgba(48,50,64,.7)}} .toggle input{{appearance:none;width:39px;height:22px;border-radius:99px;background:#555867;position:relative;cursor:pointer;transition:.2s;order:2}} .toggle input:after{{content:'';position:absolute;width:16px;height:16px;top:3px;left:3px;border-radius:50%;background:#fff;transition:.2s}} .toggle input:checked{{background:var(--purple)}} .toggle input:checked:after{{transform:translateX(17px)}} .toggle label{{margin:0;text-transform:none;font-size:13px;letter-spacing:0}}
.actions{{position:sticky;bottom:14px;margin-top:24px;background:rgba(27,28,38,.94);backdrop-filter:blur(12px);border:1px solid var(--line);padding:12px 14px;border-radius:12px;display:flex;justify-content:flex-end;z-index:10}} .save{{border:0;background:linear-gradient(135deg,#8153eb,#9b6cff);box-shadow:0 7px 22px rgba(139,92,246,.25);color:#fff;font-weight:900;padding:11px 22px;border-radius:8px;cursor:pointer}} .saved{{background:#173825;color:#75dc98;border:1px solid #285e3d;padding:12px 15px;border-radius:10px;margin-bottom:18px}}
.metric-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin-bottom:20px}} .metric{{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:19px}} .metric strong{{display:block;font-size:27px}} .metric span{{color:var(--muted)}} code{{color:var(--purple2)}}
@media(max-width:1100px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}} .bot-row{{grid-template-columns:repeat(5,170px)}}}} @media(max-width:760px){{.shell{{grid-template-columns:1fr}} aside{{position:static;height:auto;overflow:visible}} .nav-group{{display:flex;overflow-x:auto;padding-bottom:10px}} .nav-label{{display:none}} nav button{{white-space:nowrap;width:auto}} .topbar{{position:static;padding:0 16px}} main{{padding:18px 15px 40px}} .grid,.metric-row{{grid-template-columns:1fr}} .wide{{grid-column:auto}} .brand{{justify-content:center}} .server-card{{max-width:420px;margin-left:auto;margin-right:auto}}}}
</style></head><body><div class="shell"><aside><div class="brand"><div class="brand-mark">◀</div><div><b>APL B⚽T</b><small>Control Centre</small></div></div><div class="server-card"><div class="server-icon">{icon_markup}</div><div><strong>{html.escape(guild.name)}</strong><span>● Bot online</span></div></div><nav>
<div class="nav-group"><span class="nav-label">Dashboard</span><button class="active" data-tab="overview"><span class="nav-icon">▦</span>Overview</button></div>
<div class="nav-group"><span class="nav-label">Configuration</span><button data-tab="moderation"><span class="nav-icon">◉</span>Moderation</button><button data-tab="tickets"><span class="nav-icon">▣</span>Tickets & Applications</button><button data-tab="community"><span class="nav-icon">♧</span>Welcome & Goodbye</button></div>
<div class="nav-group"><span class="nav-label">Statistics</span><button data-tab="leveling"><span class="nav-icon">★</span>Leveling</button><button data-tab="tracking"><span class="nav-icon">◔</span>Tracking & Giveaways</button></div>
<div class="nav-group"><span class="nav-label">APL Management</span><button data-tab="team"><span class="nav-icon">⚽</span>Teams & Signings</button></div>
</nav></aside><div class="content"><div class="topbar"><div class="crumbs"><span>▦ Dashboard</span><span>›</span><span>{html.escape(guild.name)}</span><span>›</span><b id="crumb-current">Overview</b></div><div class="top-status"><span class="online-dot"></span>Connected</div></div><main>
<div class="bot-row"><div class="bot-choice active"><div class="bot-icon">APL</div><span>APL Bot</span><small>●</small></div><div class="bot-choice"><div class="bot-icon">⚽</div><span>Team system</span><small>+</small></div><div class="bot-choice"><div class="bot-icon">🎫</div><span>Tickets</span><small>+</small></div><div class="bot-choice"><div class="bot-icon">🛡</div><span>Moderation</span><small>+</small></div><div class="bot-choice"><div class="bot-icon">⭐</div><span>Leveling</span><small>+</small></div></div>
<div class="notice"><div class="bot-icon">APL</div><div><b>You're configuring APL Bot</b><span>Settings on this page apply to {html.escape(guild.name)} and save directly to the bot.</span></div></div>
<div class="page-title"><div><h1 id="page-heading">Overview</h1><p>Configure every part of your Discord bot in one place.</p></div><span class="status">● Live</span></div>{saved_banner}
<form method="post" action="/dashboard?token={token}">
<section class="tab active" id="overview"><div class="metric-row"><div class="metric"><strong>{guild.member_count}</strong><span>Members</span></div><div class="metric"><strong>{len(guild.text_channels)}</strong><span>Text channels</span></div><div class="metric"><strong>{len(guild.roles)}</strong><span>Roles</span></div><div class="metric"><strong>{len([m for m in guild.members if m.bot])}</strong><span>Bots</span></div></div><div class="grid"><div class="card"><div class="card-head"><span class="feature-icon">⚙</span><div><h2>Configuration</h2><p class="hint">Set channels, roles and automated features.</p></div></div><div class="card-body"><p class="hint">Choose a section from the sidebar. Your changes are stored in the same Railway database as the bot.</p></div></div><div class="card"><div class="card-head"><span class="feature-icon">⌁</span><div><h2>Discord commands</h2><p class="hint">All commands are connected.</p></div></div><div class="card-body"><p class="hint">Use <code>/commands</code> for the full list or <code>/dashboard</code> whenever you need a fresh secure link.</p></div></div><div class="card"><div class="card-head"><span class="feature-icon">✓</span><div><h2>Deployment</h2><p class="hint">Railway health and database status.</p></div></div><div class="card-body"><div class="toggle"><label>Bot connected</label><input type="checkbox" checked disabled></div><div class="toggle"><label>Settings database</label><input type="checkbox" checked disabled></div></div></div></div></section>
<section class="tab" id="moderation"><div class="grid"><div class="card"><div class="card-head"><span class="feature-icon">◉</span><div><h2>Discord AutoMod</h2><p class="hint">Stop invites and repeated-message spam.</p></div></div><div class="card-body"><div class="toggle"><input type="checkbox" name="automod_enabled"{checked('automod_enabled')}><label>Enable auto-moderation</label></div><div class="toggle"><input type="checkbox" name="automod_invites"{checked('automod_invites','1')}><label>Block Discord invite links</label></div></div></div><div class="card"><div class="card-head"><span class="feature-icon">⚠</span><div><h2>Warnings</h2><p class="hint">Choose when automatic action is applied.</p></div></div><div class="card-body"><label>Warning timeout threshold</label><input type="number" min="1" max="20" name="warn_threshold" value="{html.escape(get_value('warn_threshold','3'))}"><p class="hint">At this number of points, the member receives a one-hour timeout.</p></div></div><div class="card"><div class="card-head"><span class="feature-icon">◌</span><div><h2>Cases & Logging</h2><p class="hint">Keep staff actions easy to review.</p></div></div><div class="card-body"><label>Log channel</label><select name="log_channel_id">{channels('log_channel_id')}</select><div class="toggle"><input type="checkbox" name="ghostping_enabled"{checked('ghostping_enabled')}><label>Detect ghost pings</label></div></div></div></div></section>
<section class="tab" id="tickets"><div class="grid"><div class="card"><div class="card-head"><span class="feature-icon">▣</span><div><h2>Ticket Channels</h2><p class="hint">Where private support channels are created.</p></div></div><div class="card-body"><label>Ticket category</label><select name="ticket_category_id">{cats('ticket_category_id')}</select><label>Ticket staff role</label><select name="ticket_staff_role_id">{role_options('ticket_staff_role_id')}</select></div></div><div class="card"><div class="card-head"><span class="feature-icon">✎</span><div><h2>Applications</h2><p class="hint">Applications use ticket questions and staff access.</p></div></div><div class="card-body"><label>Panel channel</label><select name="ticket_panel_channel">{channels('ticket_panel_channel')}</select><p class="hint">Use <code>/applicationsetup</code> to add questions and <code>/ticketpanel</code> to publish.</p></div></div><div class="card"><div class="card-head"><span class="feature-icon">＋</span><div><h2>Ticket Buttons</h2><p class="hint">Create up to ten custom ticket types.</p></div></div><div class="card-body"><p class="hint">Use <code>/addticketbutton</code> for button names, colours, questions and ping roles. Existing persistent buttons continue working after restarts.</p></div></div></div></section>
<section class="tab" id="community"><div class="grid"><div class="card"><div class="card-head"><span class="feature-icon">👋</span><div><h2>Welcome Messages</h2><p class="hint">Greet new members with a branded embed.</p></div></div><div class="card-body"><label>Channel</label><select name="welcome_channel">{channels('welcome_channel')}</select><label>Message</label><textarea name="welcome_message">{html.escape(get_value('welcome_message','Welcome {user} to **{server}**!'))}</textarea></div></div><div class="card"><div class="card-head"><span class="feature-icon">↗</span><div><h2>Goodbye Messages</h2><p class="hint">Post when a member leaves.</p></div></div><div class="card-body"><label>Channel</label><select name="farewell_channel">{channels('farewell_channel')}</select><label>Message</label><textarea name="farewell_message">{html.escape(get_value('farewell_message','Goodbye {user} — thanks for being part of **{server}**.'))}</textarea></div></div><div class="card"><div class="card-head"><span class="feature-icon">{{ }}</span><div><h2>Message Variables</h2><p class="hint">Personalise community messages.</p></div></div><div class="card-body"><p class="hint"><code>{{user}}</code> mentions the member.<br><code>{{server}}</code> inserts the server name.<br><code>{{member_count}}</code> inserts the current count.</p></div></div></div></section>
<section class="tab" id="leveling"><div class="grid"><div class="card"><div class="card-head"><span class="feature-icon">★</span><div><h2>XP & Levels</h2><p class="hint">Reward members for active conversation.</p></div></div><div class="card-body"><div class="toggle"><input type="checkbox" name="leveling_enabled"{checked('leveling_enabled')}><label>Enable leveling</label></div><label>Level-up channel</label><select name="level_channel">{channels('level_channel')}</select></div></div><div class="card"><div class="card-head"><span class="feature-icon">♛</span><div><h2>Reward Roles</h2><p class="hint">Automatically grant roles at chosen levels.</p></div></div><div class="card-body"><p class="hint">Use <code>/levelroles</code> to add or remove rewards. Members earn XP at most once per minute to prevent spam farming.</p></div></div><div class="card"><div class="card-head"><span class="feature-icon">▥</span><div><h2>Leaderboards</h2><p class="hint">Show rank and server activity.</p></div></div><div class="card-body"><p class="hint"><code>/rank</code><br><code>/leaderboard</code><br><code>/setxp</code> and <code>/resetxp</code></p></div></div></div></section>
<section class="tab" id="tracking"><div class="grid"><div class="card"><div class="card-head"><span class="feature-icon">◔</span><div><h2>Message & Invite Tracking</h2><p class="hint">Power activity and invite leaderboards.</p></div></div><div class="card-body"><div class="toggle"><input type="checkbox" name="tracking_enabled"{checked('tracking_enabled','1')}><label>Enable tracking</label></div></div></div><div class="card"><div class="card-head"><span class="feature-icon">🎉</span><div><h2>Giveaways</h2><p class="hint">Reaction-based giveaways with rerolls.</p></div></div><div class="card-body"><div class="toggle"><input type="checkbox" name="giveaways_enabled"{checked('giveaways_enabled','1')}><label>Enable giveaways</label></div><p class="hint">Start one with <code>/gstart</code>.</p></div></div><div class="card"><div class="card-head"><span class="feature-icon">⭐</span><div><h2>Starboard</h2><p class="hint">Highlight popular community messages.</p></div></div><div class="card-body"><label>Starboard channel</label><select name="starboard_channel">{channels('starboard_channel')}</select><label>Required stars</label><input type="number" min="1" max="100" name="starboard_threshold" value="{html.escape(get_value('starboard_threshold','3'))}"></div></div></div></section>
<section class="tab" id="team"><div class="grid"><div class="card"><div class="card-head"><span class="feature-icon">⚽</span><div><h2>Team Management</h2><p class="hint">Configure managers and roster limits.</p></div></div><div class="card-body"><label>Manager role</label><select name="manager_role_id">{role_options('manager_role_id')}</select><label>Roster cap</label><input type="number" min="1" max="100" name="roster_cap" value="{html.escape(get_value('roster_cap','22'))}"></div></div><div class="card"><div class="card-head"><span class="feature-icon">↗</span><div><h2>Announcements</h2><p class="hint">Control signing and fixture destinations.</p></div></div><div class="card-body"><label>Signings channel</label><select name="signings_channel_id">{channels('signings_channel_id')}</select><label>Game-time channel</label><select name="gametime_channel_id">{channels('gametime_channel_id')}</select></div></div><div class="card"><div class="card-head"><span class="feature-icon">★</span><div><h2>Team of the Week</h2><p class="hint">Private screenshot upload sessions.</p></div></div><div class="card-body"><label>TOTW category</label><select name="totw_category_id">{cats('totw_category_id')}</select><p class="hint">Admins can start a private session with <code>/totw</code>.</p></div></div></div></section>
<div class="actions"><button class="save" type="submit">Save changes</button></div></form></main></div></div>
<script>const buttons=[...document.querySelectorAll('nav button')],tabs=[...document.querySelectorAll('.tab')],heading=document.getElementById('page-heading'),crumb=document.getElementById('crumb-current');buttons.forEach(b=>b.onclick=()=>{{buttons.forEach(x=>x.classList.remove('active'));tabs.forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.tab).classList.add('active');const title=b.textContent.replace(/^[^A-Za-z]+/,'').trim();heading.textContent=title;crumb.textContent=title;localStorage.setItem('apl-tab',b.dataset.tab)}});const saved=localStorage.getItem('apl-tab');if(saved)document.querySelector(`[data-tab="${{saved}}"]`)?.click();</script></body></html>"""


async def setup_dashboard(bot, connect_db, get_setting, set_setting, is_admin, member_is_admin, reply):
    with connect_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS dashboard_sessions (token TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL)")

    async def session_for(request):
        token = request.query.get("token", "")
        with connect_db() as db:
            row = db.execute("SELECT * FROM dashboard_sessions WHERE token=? AND expires_at>?", (token, int(time.time()))).fetchone()
        if not row:
            return None, None, token
        guild = bot.get_guild(row["guild_id"])
        member = guild.get_member(row["user_id"]) if guild else None
        if not guild or not member or not member_is_admin(member):
            return None, None, token
        return guild, member, token

    def read_value(guild, key, default=""):
        direct_keys = {"log_channel_id","ticket_category_id","ticket_staff_role_id","manager_role_id","roster_cap","signings_channel_id","gametime_channel_id","totw_category_id"}
        prefix = f"guild:{guild.id}:" if key in direct_keys else f"guild:{guild.id}:byronic:"
        return get_setting(prefix + key, default)

    async def dashboard_get(request):
        guild, _, token = await session_for(request)
        if not guild:
            return web.Response(text="This dashboard link is invalid or expired. Run /dashboard again in Discord.", status=403, content_type="text/plain")
        return web.Response(text=page(guild, token, lambda key, default="": read_value(guild,key,default), request.query.get("saved") == "1"), content_type="text/html")

    async def dashboard_post(request):
        guild, _, token = await session_for(request)
        if not guild:
            raise web.HTTPForbidden(text="This dashboard link is invalid or expired.")
        form = await request.post()
        checkboxes = {"automod_enabled","automod_invites","ghostping_enabled","leveling_enabled","tracking_enabled","giveaways_enabled"}
        direct_keys = {"log_channel_id","ticket_category_id","ticket_staff_role_id","manager_role_id","roster_cap","signings_channel_id","gametime_channel_id","totw_category_id"}
        allowed = direct_keys | checkboxes | {"warn_threshold","welcome_channel","welcome_message","farewell_channel","farewell_message","level_channel","ticket_panel_channel","starboard_channel","starboard_threshold"}
        for key in allowed:
            value = "1" if key in checkboxes and key in form else ("0" if key in checkboxes else str(form.get(key,"")))
            prefix = f"guild:{guild.id}:" if key in direct_keys else f"guild:{guild.id}:byronic:"
            set_setting(prefix + key, value)
        raise web.HTTPFound(f"/dashboard?token={token}&saved=1")

    async def health(_):
        return web.json_response({"ok": True, "bot_ready": bot.is_ready(), "guilds": len(bot.guilds)})

    @bot.tree.command(name="dashboard", description="Open the private bot configuration dashboard")
    async def dashboard_command(interaction: discord.Interaction):
        if not is_admin(interaction):
            return await reply(interaction, "Only server administrators can open the dashboard.")
        base_url = dashboard_base_url()
        if not base_url:
            return await reply(interaction, "The dashboard needs `DASHBOARD_URL` in Railway Variables, for example `https://your-service.up.railway.app`.")
        token = secrets.token_urlsafe(32)
        with connect_db() as db:
            db.execute("DELETE FROM dashboard_sessions WHERE expires_at<=?", (int(time.time()),))
            db.execute("INSERT INTO dashboard_sessions VALUES(?,?,?,?)", (token,interaction.guild.id,interaction.user.id,int(time.time())+1800))
        embed = discord.Embed(title="APL Bot Dashboard", description="Use the button below to configure this server. The private link expires in **30 minutes**.", color=0x5865F2)
        embed.set_footer(text="APL Bot • Administrator access")
        view = discord.ui.View(); view.add_item(discord.ui.Button(label="Open Dashboard", style=discord.ButtonStyle.link, url=f"{base_url}/dashboard?token={token}"))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_get("/", lambda _: web.HTTPFound("/health"))
    app.router.add_get("/health", health)
    app.router.add_get("/dashboard", dashboard_get)
    app.router.add_post("/dashboard", dashboard_post)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080")))
    await site.start()
    bot.dashboard_runner = runner
