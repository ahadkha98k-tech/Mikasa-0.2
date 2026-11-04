# Mikasa 2.0

## Deploy on Render (free web service, no database)
1. Push this folder to GitHub → Render → New Web Service (or Blueprint using render.yaml).
2. Discord Developer Portal → Bot → enable **Server Members Intent** AND **Message Content Intent**.
3. Env vars: `TOKEN`, `DASHBOARD_PASSWORD` (long & random).
   Optional AI: `GROQ_API_KEY` (or OPENAI / OPENROUTER / XAI / ANTHROPIC key).

## Data centre = a Discord server (`/set data centre`)
Render's free plan erases local files on every restart, so Mikasa keeps ALL its data in a private Discord server chosen by the bot owner.
1. Create/choose a server only you (and people you trust) are in, and invite Mikasa with **Administrator**.
2. Run `/set data centre` there (bot owner only). Mikasa builds:
   - **🌐 Global Data** - `#global-data` (bot-wide data) and `#centre-meta` (marker used to find the centre again)
   - **🌸 \<Server name\>** - one category per server Mikasa is in, each with `#settings` `#users` (XP, coins, warnings) `#ai-memory` `#whitelists` `#logs`
3. That's it - after any restart/redeploy Mikasa finds the centre again by itself. No env var needed.

- Run it again in another server to move the centre there (current data is copied over). `restore:True` loads the data already stored in that server instead.
- New servers get their category automatically; renaming a server renames its category.
- `/dbstatus` (owner only) shows the centre, category progress, unsaved changes, last save and errors.
- Upgrading from the old `/setdata`/`#mikasa-db`? The old data is still read on startup; run `/set data centre` once to migrate it.
- Never delete those channels or the bot's messages in them. Saved every ~30s and on shutdown.
- Discord allows 500 channels per server and each bot server uses 5, so one data centre holds about 98 servers.

## Dashboard checklist
- `dashboard.html` must sit next to `Mikasa.py` in the repo (the bot only serves it). The startup log warns if it's missing.
- `DASHBOARD_PASSWORD` must be set in Render -> Environment (the blueprint doesn't fill it in). Login is locked for 10 min after 5 wrong tries.
- The page must send the header `X-Requested-With: mikasa` on POST requests.
- Open `https://<your-app>.onrender.com/health` - if that says `ok` the web server is up.

## Staying awake
Free services sleep after 15 min without traffic. Mikasa pings itself every 10 min;
for extra safety add a free UptimeRobot monitor on `https://<your-app>.onrender.com/health`.
