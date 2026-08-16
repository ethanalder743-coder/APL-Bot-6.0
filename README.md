# Discord Team Bot

A beginner-friendly Discord.py bot with `/addteam`, `/offer`, and `/release`.

## What it does

- `/addteam manager team_role` links that manager to exactly one team role and gives them both roles. Running it again updates their saved team.
- `/offer player` lets a registered manager offer only their linked team role by DM.
- Accepting assigns the team role, edits the original offer DM into an **Offer Accepted** embed, and posts a separate embed in the signings channel. It does not send another DM or a plain public acceptance message.
- `/release player` lets a manager remove only their linked team role from a player.
- Teams and pending offers are saved in SQLite, and offer buttons survive restarts.
- `/ticketpanel` lets an administrator post a Discord-only ticket panel. Members can create private tickets; staff can claim and close them.
- `/totw` lets an administrator open a private upload channel, upload any number of Player Performance screenshots, calculate a balanced 11-player Team of the Week, and receive a generated PNG.

## Discord setup

In the Discord Developer Portal, enable **Server Members Intent** for the bot. Invite it with the `bot` and `applications.commands` scopes. Give it **Manage Roles**, **View Channels**, and **Send Messages** permissions. Its bot role must sit above the manager and team roles.

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
```

`ROSTER_CAP` and `LEAGUE_NAME` control the heading and roster display in the signing embed. For multiple bot owners, separate IDs with commas. Never commit the real `.env` file or bot token.

The three category/staff IDs configure where ticket and TOTW channels are created. They are optional, but recommended. `/totw` is visible in Discord's command list to members, but the bot only allows server administrators to run it; Discord does not support hiding one slash command from selected members without configuring command permissions in **Server Settings → Integrations**.

### Keeping SQLite data on Railway

Create a Railway volume mounted at `/data`, then add:

```env
DATABASE_PATH=/data/teams.db
```

Without a volume, Railway's local filesystem may be replaced during a redeploy.

## Deploy

Upload the contents of this folder to the root of a GitHub repository, connect it to Railway, add the variables, and deploy. `railway.json` starts the bot with `python bot.py`.

The included `.python-version` pins Railway to Python 3.12 because the OCR package used by `/totw` does not support Python 3.13 yet. You can also add `RAILPACK_PYTHON_VERSION=3.12` in Railway Variables if Railway has cached an older build configuration.

If you keep this inside another repository folder, set Railway's **Root Directory** to the exact folder containing `bot.py` and `requirements.txt` (for example `/discord-team-bot`).

## Use

1. A user listed in `OWNER_IDS` runs `/addteam`.
2. That manager runs `/offer` and chooses a player.
3. The player receives the original offer DM and presses Accept or Deny.
4. On Accept, the role is assigned and the announcement appears in `SIGNINGS_CHANNEL_ID`.
