# Discord Team Bot

A beginner-friendly Discord.py bot with `/addteam`, `/offer`, and `/release`.

## What it does

- `/addteam manager team_role` registers a team and gives the manager both roles.
- `/offer player` lets a registered manager send an Accept/Deny offer by DM.
- Accepting assigns the team role and posts an **Offer Accepted** embed in the signings channel. It does not send another confirmation DM.
- `/release player` lets a manager remove their own team role from a player.
- Teams and pending offers are saved in SQLite, and offer buttons survive restarts.

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
```

For multiple bot owners, separate IDs with commas. Never commit the real `.env` file or bot token.

### Keeping SQLite data on Railway

Create a Railway volume mounted at `/data`, then add:

```env
DATABASE_PATH=/data/teams.db
```

Without a volume, Railway's local filesystem may be replaced during a redeploy.

## Deploy

Upload the contents of this folder to the root of a GitHub repository, connect it to Railway, add the variables, and deploy. `railway.json` starts the bot with `python bot.py`.

If you keep this inside another repository folder, set Railway's **Root Directory** to the exact folder containing `bot.py` and `requirements.txt` (for example `/discord-team-bot`).

## Use

1. A user listed in `OWNER_IDS` runs `/addteam`.
2. That manager runs `/offer` and chooses a player.
3. The player receives the original offer DM and presses Accept or Deny.
4. On Accept, the role is assigned and the announcement appears in `SIGNINGS_CHANNEL_ID`.
