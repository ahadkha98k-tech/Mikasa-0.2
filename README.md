# Mikasa 2.0

## Deploy on Render (free web service, no database)
1. Push this folder to GitHub → Render → New Web Service (or Blueprint using render.yaml).
2. Discord Developer Portal → Bot → enable **Server Members Intent** AND **Message Content Intent**.
3. Env vars: `TOKEN`, `DASHBOARD_PASSWORD` (long & random).
   Optional AI: `GROQ_API_KEY` (or OPENAI / OPENROUTER / XAI / ANTHROPIC key).

## Global database = a Discord server (`/setdata`)
Render's free plan erases local files on every restart, so Mikasa stores everything
(settings, XP, coins, warnings for ALL servers) in a Discord server picked by the bot owner.
1. Create/choose a server only you (and people you trust) are in, and invite Mikasa with **Administrator**.
2. Run `/setdata` there (bot owner only). Mikasa makes a hidden `#mikasa-db` channel and starts saving into it.
3. That's it - after any restart/redeploy Mikasa finds that server again by itself. No env var needed.

- `/setdata` again in another server moves the database there (current data is copied over).
  Use `/setdata restore:True` to load the data already stored in that server instead.
- `/dbstatus` (owner only) shows which server is the database, last save time and errors.
- Never delete `#mikasa-db` or the bot's messages in it. Saved every ~30s and on shutdown.

## Staying awake
Free services sleep after 15 min without traffic. Mikasa pings itself every 10 min;
for extra safety add a free UptimeRobot monitor on `https://<your-app>.onrender.com/health`.
