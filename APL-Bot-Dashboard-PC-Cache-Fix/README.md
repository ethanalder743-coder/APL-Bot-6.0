# APL Discord Bot + Control Dashboard

This Railway-ready project combines the existing APL team-management bot with a working Byronic-inspired server-management command set and a private configuration dashboard. It uses APL branding and original code; it does not copy Byronic's private code or logo.

## Included systems

- Existing APL teams, offers, signings, releases, rosters, tickets, TOTW and Game Time
- Moderation cases, warnings, kick/ban, timeouts, clean chat and channel lockdown
- Auto-moderation for invite links and spam
- XP, levels, leaderboards and reward roles
- Message and invite tracking
- Giveaways and starboard
- Welcome, farewell and ghost-ping logging
- Application tickets with questions
- Private FlaviBot-inspired single-page dashboard with a fixed sidebar, breadcrumb navigation, configuration cards and working responsive tabs
- Persistent SQLite storage across Railway redeployments

Use `/commands` for the complete categorized command list. Use `/dashboard` as a server administrator to receive a private dashboard button. Dashboard links expire after 30 minutes and are checked against the member's current Discord administrator permission.

## Railway deployment

1. Replace the old GitHub project files with this package.
2. Connect the repository to Railway.
3. Add the variables from `.env.example`.
4. Mount a Railway volume at `/data`.
5. Set `DATABASE_PATH=/data/teams.db`.
6. Deploy and wait for the service to show healthy.
7. In Railway, open **Settings → Networking → Generate Domain**.
8. Set `DASHBOARD_URL` to the complete generated address, such as `https://apl-bot-production.up.railway.app`.
9. Redeploy once, then use `/dashboard` in Discord.

Railway supplies `PORT` automatically. Do not add your own `PORT` variable.

## Required Discord settings

In the Discord Developer Portal, enable:

- Server Members Intent
- Message Content Intent

Invite the bot with the `bot` and `applications.commands` scopes. For every included feature, it needs Manage Roles, Manage Channels, Manage Messages, Moderate Members, Kick Members, Ban Members, View Audit Log, Send Messages, Embed Links, Attach Files, Read Message History and Add Reactions. The bot role must be above roles it assigns or moderates.

## Important variables

- `DISCORD_TOKEN`: Discord bot token
- `OWNER_IDS`: your Discord user ID (comma-separated for multiple global owners)
- `GUILD_ID`: optional main server ID
- `DASHBOARD_URL`: generated Railway HTTPS domain
- `DATABASE_PATH`: `/data/teams.db` when using the Railway volume
- `SIGNINGS_CHANNEL_ID`: fallback signings channel
- `ROSTER_CAP`: fallback team roster cap

Most channel and role settings can be changed later through `/dashboard` without editing Railway variables.

## Command syncing

The bot syncs global slash commands at startup. Discord can take up to one hour to show newly added global commands, although it is normally faster. Check Railway logs for `Synced ... global commands`. If the bot cannot sync, confirm that it was invited using the `applications.commands` scope.

## Saved data

All configuration, teams, offers, cases, XP, counts and dashboard sessions use the same SQLite database. A mounted `/data` Railway volume keeps the data when code is updated or the service redeploys.
