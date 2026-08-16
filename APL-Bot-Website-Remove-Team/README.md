# Discord Team Bot

A beginner-friendly Discord.py bot with `/addteam`, `/offer`, and `/release`.

## Included APL website

The same Railway service now hosts the APL League Reload website and runs the Discord bot. The website automatically imports team names registered with `/addteam`. Website admin changes are stored in the same persistent SQLite database instead of only one browser.

Website access uses email invitations instead of a shared password. `WEB_OWNER_EMAIL` is the permanent website owner. Use `/websiteaccess` privately in Discord for the owner's first sign-in. In the website admin console, enter another administrator's email and press **Send invitation**; the owner's normal email app opens with the secure link and message ready, so the owner only needs to press Send. The invited email remains an approved administrator after clicking the link.

## What it does

- `/addteam manager team_role` links that manager to exactly one team role and gives them both roles. Running it again updates their saved team.
- `/removeteam team_role` removes the saved team, its website entry, bot-tracked roster, and pending offers without deleting the actual Discord role.
- `/offer player` lets a registered manager offer only their linked team role by DM.
- Accepting assigns the team role, edits the original offer DM into an **Offer Accepted** embed, and posts a separate embed in the signings channel. It does not send another DM or a plain public acceptance message.
- `/release player` lets a manager remove only their linked team role from a player.
- Teams, bot-signed roster members, and pending offers are saved in SQLite, and offer buttons survive restarts. Roster totals count players signed by this bot instead of unrelated members who happen to hold the selected Discord role.
- `/addticketbutton` lets an administrator save up to 25 coloured/emoji ticket buttons. Each ticket type can ask up to five questions.
- `/setticketpings` assigns up to five roles to a ticket type. Those roles can see the ticket and are mentioned when it opens; `/clearticketpings` removes them.
- `/deleteticketbutton` removes a saved ticket type.
- `/ticketpanel` posts the configured Discord-only panel. Opening a ticket shows the member's answers in an embed, with Claim Ticket and Close Ticket controls.
- `/totw` lets an administrator open a private upload channel, upload any number of Player Performance screenshots, calculate a balanced 11-player Team of the Week, and receive a generated PNG.
- `/commands` displays a role-aware Player Commands and Manager Commands help menu.
- Players can use `/myoffers`, `/teaminfo`, `/teams`, and `/roster`.
- Managers can also use `/canceloffer`, `/teamoffers`, `/promote`, and `/demote` alongside `/offer` and `/release`.
- Manager signing commands only work in the channel selected with `/setsigningschannel` (bot admins can bypass this restriction).
- Owners can use `/addadmin` and `/removeadmin` to manage who can use APL administration tools.
- Every Discord server owner automatically has the highest bot access in their own server. Users in `OWNER_IDS` are global bot owners and can administer the bot in every server it joins.
- `/setsigningschannel` chooses the signing-announcement channel separately for each server.
- `/setrostercap` chooses each server's player limit. Managers and bots are not counted, and full teams cannot accept more offers.
- `/setmanagerrole` chooses the manager role separately for each server.
- `/allrosters` gives admins clear team-by-team embeds containing the manager mention, player count, and every player mention.
- `/rules` and `/managerlist` publish polished embeds with the exact text supplied by an admin.
- `/setlogchannel` saves an embed-only audit log channel for moderation, tickets, teams, offers, joins, leaves, and other actions.
- Moderation includes `/warn`, `/mute`, `/unmute`, `/kick`, `/ban`, and `/purge`. Ban logs show only the banned member and reason, not the moderator.
- `/setwelcome` configures a welcome embed. Placeholders: `{user}`, `{server}`, and `{member_count}`.
- `/rolesaver` enables role restoration. `/rolesaverexclude` selects roles that must not return, and `/rolesaverallow` removes an exclusion.
- `/poll` posts a reaction poll with two to five choices.
- `?stick your message` creates a repeating sticky embed in that channel; `?unstick` removes it.
- `/setgametimechannel` lets an owner/admin choose the only channel used for game-time announcements.
- `/gametimerole` grants or removes `/gametime` access for selected roles, and `/gametimeroles` lists them.
- `/gametime` lets authorised members submit an opponent, date, kick-off time and optional note. The bot posts a fixture embed in the configured channel.

## Discord setup

In the Discord Developer Portal, enable **Server Members Intent** for the bot. Invite it with the `bot` and `applications.commands` scopes. Give it **Manage Roles**, **View Channels**, and **Send Messages** permissions. Its bot role must sit above the manager and team roles.

Also enable **Message Content Intent** in the Developer Portal. It is required for the `?stick` and `?unstick` text commands. Recommended permissions for the complete bot are Manage Roles, Manage Channels, Manage Messages, Moderate Members, Kick Members, Ban Members, View Audit Log, Read Message History, Attach Files, Embed Links, and Add Reactions.

Turn on Developer Mode in Discord so you can copy IDs.

## Railway variables

Add these under your Railway service's **Variables** page:

```env
DISCORD_TOKEN=your_bot_token
GUILD_ID=your_server_id
MANAGER_ROLE_ID=your_manager_role_id
OWNER_IDS=your_user_id
SIGNINGS_CHANNEL_ID=your_signings_channel_id
ROSTER_CAP=22
LEAGUE_NAME=APL | RELOAD | FC26
TICKET_CATEGORY_ID=your_ticket_category_id
TICKET_STAFF_ROLE_ID=your_ticket_staff_role_id
TOTW_CATEGORY_ID=your_private_totw_category_id
HEAD_COACH_ROLE_ID=your_head_coach_role_id
ASSISTANT_COACH_ROLE_ID=your_assistant_coach_role_id
WEB_OWNER_EMAIL=you@example.com
WEB_BASE_URL=https://your-generated-domain.up.railway.app
```

`MANAGER_ROLE_ID`, `SIGNINGS_CHANNEL_ID`, and `ROSTER_CAP` are defaults for the first server. Its owner or an admin can override them with `/setmanagerrole`, `/setsigningschannel`, and `/setrostercap`; those choices are saved separately for every server. `LEAGUE_NAME` controls the signing embed heading. For multiple global bot owners, separate `OWNER_IDS` with commas. Never commit the real `.env` file or bot token.

The three category/staff IDs configure where ticket and TOTW channels are created. They are optional, but recommended. `/totw` is visible in Discord's command list to members, but the bot only allows server administrators to run it; Discord does not support hiding one slash command from selected members without configuring command permissions in **Server Settings → Integrations**.

### Keeping SQLite data on Railway

Create a Railway volume mounted at `/data`, then add:

```env
DATABASE_PATH=/data/teams.db
```

Without a volume, Railway's local filesystem may be replaced during a redeploy.

## Deploy

Upload the contents of this folder to the root of a GitHub repository, connect it to Railway, add the variables, and deploy. `railway.json` starts the bot with `python bot.py`.

After the deployment is healthy, open the Railway service, select **Settings → Networking → Generate Domain**, and use the generated HTTPS address for the website. The bot and website use the same deployment and `/data/teams.db` volume, so a second Railway service is not required.

Copy that complete generated address into `WEB_BASE_URL`, put your own email in `WEB_OWNER_EMAIL`, and redeploy. No Gmail password or email-provider setup is required. Run `/websiteaccess` in Discord, open its private link, then invite other administrators from the website panel.

The included `.python-version` pins Railway to Python 3.12 because the OCR package used by `/totw` does not support Python 3.13 yet. You can also add `RAILPACK_PYTHON_VERSION=3.12` in Railway Variables if Railway has cached an older build configuration.

If you keep this inside another repository folder, set Railway's **Root Directory** to the exact folder containing `bot.py` and `requirements.txt` (for example `/discord-team-bot`).

## Use

1. The server owner runs `/setmanagerrole`, `/setsigningschannel`, and `/setrostercap` once.
2. The server owner, a server admin, or a global owner runs `/addteam`.
3. That manager runs `/offer` and chooses a player.
4. The player receives the original offer DM and presses Accept or Deny.
5. On Accept, the role is assigned and the announcement appears in that server's configured signings channel.
