# Discord Team Bot (discord.py + Railway)

A beginner-friendly Discord bot with `/addteam`, `/offer`, and `/release`. Team settings and pending offers are stored in SQLite. Offer buttons continue working after a bot restart.

## What the commands do

- `/addteam manager team_role` — server administrators or IDs in `OWNER_IDS` configure a team. The manager receives the global Manager role and the selected team role.
- `/offer player team_role` — only that team's configured manager can use it. The player gets an embed by DM with **Accept** and **Deny** buttons. Accept adds the team role; Deny changes nothing. The manager is notified by DM when possible.
- `/release player team_role` — only that team's manager can use it, and only on someone holding that role. It removes the selected team role only.

## 1. Create and invite the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and open **Bot**.
2. Enable **Server Members Intent** under Privileged Gateway Intents.
3. On **OAuth2 → URL Generator**, select `bot` and `applications.commands`.
4. Give it **Manage Roles**, then use the generated URL to invite it.
5. In your server's role list, drag the bot's role above the global Manager role and every team role it will manage. Discord never lets a bot manage roles above its own highest role.

Enable Developer Mode in Discord (**User Settings → Advanced**). You can then right-click the server, users, and roles to copy their IDs.

## 2. Test locally (optional)

Python 3.11 or newer is recommended.

```text
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`, then run:

```text
python -m bot
```

Never upload `.env` or reveal your bot token. If it leaks, reset it immediately in the Developer Portal.

## 3. Upload to GitHub

Create an empty GitHub repository, unzip this project, and upload all its files. `.env` is ignored; `.env.example` is safe to upload because it contains placeholders.

## 4. Deploy on Railway

1. In Railway, choose **New Project → Deploy from GitHub repo** and select the repository.
2. Under **Variables**, add:

| Variable | Required? | Example / purpose |
|---|---:|---|
| `DISCORD_TOKEN` | Yes | Bot token from the Developer Portal |
| `GUILD_ID` | Yes | Server ID; commands sync there immediately |
| `MANAGER_ROLE_ID` | Yes | ID of your existing global Manager role |
| `OWNER_IDS` | Recommended | Your user ID; comma-separate multiple IDs |
| `LOG_LEVEL` | No | `INFO` |
| `AUTO_MIGRATE` | No | `true` (leave true) |
| `BACKUP_DIR` | No | `backups` (reserved for your backup workflow) |

The included `railway.json` and `Procfile` both start the worker with `python -m bot`. No web server or public domain is needed.

### Persistent SQLite on Railway

SQLite is a file, so attach a Railway Volume and mount it at `/app/data`. The bot writes `/app/data/bot.db` when Railway runs the project from `/app`. Without a volume, redeploying may erase team and pending-offer data.

`BACKUP_DIR` does not itself create cloud backups; it is included only as a conventional location for a future backup job. A Railway Volume is the important persistence step.

## Variables from database/web templates you can delete

This bot uses SQLite and does not run a website. Delete or ignore template variables such as:

- `DATABASE_URL`, `POSTGRES_URL`, `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`
- `NODE_ENV`, `WEB_HOST`, `PORT`, `HOST`
- Any Redis, MySQL, MongoDB, or public-domain variables

Only keep them if you later add and actually use those services. Railway may provide its own internal variables; you do not need to copy them into this bot.

## Troubleshooting

- **Commands do not appear:** confirm `GUILD_ID`, make sure the invite included `applications.commands`, and restart the deployment.
- **Missing Access / role errors:** give the bot Manage Roles and move its role above Manager and team roles.
- **Player receives no offer:** they may have DMs from server members disabled.
- **Members cannot be selected:** enable Server Members Intent in the Developer Portal and restart.
- **Data disappeared:** attach a volume at `/app/data` before relying on SQLite in production.

The bot reports expected errors privately (ephemeral messages) and writes unexpected errors to Railway logs.
