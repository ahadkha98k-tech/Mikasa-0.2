"""Mikasa 2.0 - moderation/community bot + pink anime dashboard (Render web service)."""
import os, re, io, json, time, hmac, hashlib, secrets, signal, asyncio, random
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
from aiohttp import web

TOKEN = os.getenv("TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DASH_PASS = os.getenv("DASHBOARD_PASSWORD", "")
SECRET = (os.getenv("SESSION_SECRET") or secrets.token_hex(32)).encode()
DATA = Path(os.getenv("DATA_DIR", "data")); DATA.mkdir(exist_ok=True)
PINK = 0xFF8FC7
BASE = Path(__file__).parent

# ---------- storage: a Discord server is the DATA CENTRE (chosen with /set data centre) ----------
# Render's free plan wipes local files on restart, so everything lives in a private "data centre" server:
#   🌐 Global Data   ->  #global-data (bot-wide data)  #centre-meta (marker the bot uses to find the centre again)
#   🌸 <Guild name>  ->  #settings #users #ai-memory #whitelists #logs      (one category per server Mikasa is in)
# Each channel holds ONE message with a JSON attachment that is edited in place. After a restart the bot finds its
# channels again by their topic ("mikasa:<guild id>:<kind>"), so no env var or external database is needed.
MARK, META, GKEY = "mikasa-centre:", "centre-meta", "global"
GUILD_KINDS = ("settings", "users", "ai-memory", "whitelists", "logs")
GLOBAL_KINDS = ("global-data", META)
TOPIC_RE = re.compile(r"^mikasa:([0-9a-z]+):([a-z-]+)")
cloud = {"gid": None, "err": None, "saved": 0, "prov": None, "legacy": None}
db_ready = asyncio.Event(); chans = {}; plock = asyncio.Lock(); _tasks = set()

def spawn(coro):
    t = asyncio.create_task(coro); _tasks.add(t); t.add_done_callback(_tasks.discard); return t

class Store:
    """One kind of data (settings, users, ...). d = {guild id: data}; each guild's data is its own file in its own channel."""
    all = []
    def __init__(self, kind):
        self.kind, self.p = kind, DATA / f"{kind}.json"
        self.msgs, self.dirty = {}, set()
        try: self.d = json.loads(self.p.read_text("utf-8"))
        except Exception: self.d = {}
        Store.all.append(self)
    def save(self, key): self.dirty.add(str(key))      # flushed to Discord every ~30s + on shutdown
    def g(self, key, default=None):
        return self.d.setdefault(str(key), {} if default is None else default)
SET, USERS, AIMEM, WL, LOGS, GLOBAL = (Store(k) for k in ("settings", "users", "ai-memory", "whitelists", "logs", "global-data"))

def norm_users(d):   # old layout was {"guild:user": data}; new layout is {guild: {user: data}}
    out = {}
    for k, v in d.items():
        if ":" in k: gid, uid = k.split(":", 1); out.setdefault(gid, {})[uid] = v
        else: out[k] = v
    return out
USERS.d = norm_users(USERS.d)

def centre(): return bot.get_guild(cloud["gid"]) if cloud["gid"] else None

async def read_json(s, m):
    try: return json.loads(await m.attachments[0].read())
    except ValueError: print(f"WARNING: a {s.kind} file in the data centre is corrupt; starting it empty"); return {}

async def centre_scan(g):
    """Return {(key, kind): (channel, latest data message or None)} for every Mikasa channel in server g."""
    found, sem = {}, asyncio.Semaphore(4)
    async def one(ch, key, kind):
        async with sem:
            msg = None
            try:
                async for m in ch.history(limit=50):          # newest first
                    if m.author.id == bot.user.id and m.attachments and m.content == MARK + kind: msg = m; break
            except discord.Forbidden: return
            found[(key, kind)] = (ch, msg)
    jobs = []
    for ch in g.text_channels:
        mt = TOPIC_RE.match(ch.topic or "")
        if mt and mt.group(2) != META and ch.permissions_for(g.me).read_message_history: jobs.append(one(ch, *mt.groups()))
    await asyncio.gather(*jobs)
    return found

async def adopt(found, restore):
    """Hook the stores up to the data centre. restore=True loads what is stored there; False copies current data into it."""
    chans.clear(); by = {s.kind: s for s in Store.all}; loaded = defaultdict(dict)
    for s in Store.all: s.msgs.clear()
    for (key, kind), (ch, msg) in found.items():
        chans[(key, kind)] = ch
        if (s := by.get(kind)):
            s.msgs[key] = msg
            if restore and msg: loaded[kind][key] = await read_json(s, msg)
    for s in Store.all:
        if restore and loaded[s.kind]: s.d = loaded[s.kind]
        for key in s.d:
            if not (restore and s.msgs.get(key)): s.dirty.add(key)

async def legacy_load():
    """Pick up data saved by the old single-channel #mikasa-db layout so nothing is lost when upgrading."""
    best = None
    for g in bot.guilds:
        ch = discord.utils.get(g.text_channels, name="mikasa-db")
        if not ch or not ch.permissions_for(g.me).read_message_history: continue
        files, meta = {}, None
        try:
            async for m in ch.history(limit=200, oldest_first=True):
                if m.author.id != bot.user.id: continue
                if m.content == "mikasa-data:meta": meta = m
                elif m.attachments and m.content.startswith("mikasa-data:"): files[m.content[12:]] = m
        except discord.Forbidden: continue
        when = meta.created_at if meta else ch.created_at
        if files and (not best or when > best[2]): best = (g, files, when)
    if not best: return
    g, files, _ = best
    for name, s in (("settings", SET), ("users", USERS)):
        if name in files:
            try: s.d = json.loads(await files[name].attachments[0].read())
            except ValueError: pass
    USERS.d = norm_users(USERS.d); cloud["legacy"] = g.name
    print(f"Loaded OLD-format data from {g.name}. Run /set data centre to move it into the new data centre.")

async def db_load():
    """Find the data centre (newest marker written by this bot) and load everything from it."""
    best = None
    for g in bot.guilds:
        mc = next((c for c in g.text_channels if (c.topic or "").startswith(f"mikasa:{GKEY}:{META}")), None)
        if not mc or not mc.permissions_for(g.me).read_message_history: continue
        try:
            async for m in mc.history(limit=20):
                if m.author.id == bot.user.id and m.content == MARK + "meta":
                    if not best or m.created_at > best[1]: best = (g, m.created_at)
                    break
        except discord.Forbidden: continue
    if not best:
        await legacy_load(); print("No data centre set yet - run /set data centre. Data is lost on restart until then."); return
    g = best[0]; await adopt(await centre_scan(g), True)
    cloud["gid"], cloud["err"] = g.id, None
    print(f"Data centre: {g.name}, {len(chans)} channel(s) found")

def cat_name(key):
    if key == GKEY: return "🌐 Global Data"
    g = bot.get_guild(int(key)); name = g.name if g else GLOBAL.g(GKEY).get("guilds", {}).get(key, key)
    return f"🌸 {name}"[:100]

def locked(cg):
    return {cg.default_role: discord.PermissionOverwrite(view_channel=False),
            cg.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)}

async def ensure(key):
    """Make sure the category + channels for one server (or the global data) exist in the data centre."""
    cg = centre()
    if not cg: raise RuntimeError("no data centre set")
    kinds = GLOBAL_KINDS if key == GKEY else GUILD_KINDS
    async with plock:
        live = {kd: c for (k, kd), c in chans.items() if k == key and cg.get_channel(c.id)}
        cat = next((c.category for c in live.values() if c.category), None); want = cat_name(key)
        if len(live) == len(kinds) and cat and cat.name == want: return
        if cat is None: cat = await cg.create_category(want, overwrites=locked(cg), reason="Mikasa data centre")
        elif cat.name != want: await cat.edit(name=want, reason="Server renamed")
        for kd in kinds:
            if kd not in live:
                chans[(key, kd)] = await cg.create_text_channel(kd, category=cat, overwrites=locked(cg),
                    topic=f"mikasa:{key}:{kd} | Mikasa data - do not delete or post here.", reason="Mikasa data centre")

async def chan_for(key, kind):
    c, cg = chans.get((key, kind)), centre()
    if not c or not cg.get_channel(c.id): await ensure(key); c = chans[(key, kind)]
    return c

async def provision_all():
    """Create/refresh one category per server Mikasa is in (also renames categories when a server is renamed)."""
    cg = centre()
    if not cg: return
    names = GLOBAL.g(GKEY).setdefault("guilds", {}); todo = list(bot.guilds); cloud["prov"] = [0, len(todo)]
    for g in todo:
        if names.get(str(g.id)) != g.name: names[str(g.id)] = g.name; GLOBAL.save(GKEY)
        try: await ensure(str(g.id))
        except Exception as e: cloud["err"] = repr(e); print("Provisioning failed:", repr(e))
        cloud["prov"][0] += 1

def record(guild, title, text=""):   # moderation / bot log history, kept in the guild's #logs channel (last 500)
    L = LOGS.g(guild.id).setdefault("entries", [])
    L.append({"t": int(time.time()), "title": str(title or "")[:100], "text": str(text or "")[:300]}); del L[:-500]; LOGS.save(guild.id)

def wl(gid):   # per-server whitelist storage (users / roles). Saved in the guild's #whitelists channel.
    w = WL.g(gid); w.setdefault("users", []); w.setdefault("roles", []); return w

async def cloud_flush():
    for s in Store.all:
        if not s.dirty: continue
        keys, s.dirty = list(s.dirty), set()
        try: s.p.write_text(json.dumps(s.d), "utf-8")
        except OSError: pass
        if not centre(): continue
        for key in keys:
            raw = json.dumps(s.d.get(key, {})).encode()
            try:
                if (m := s.msgs.get(key)):
                    try: s.msgs[key] = await m.edit(content=MARK + s.kind, attachments=[discord.File(io.BytesIO(raw), f"{s.kind}.json")]); cloud["saved"] = int(time.time()); continue
                    except discord.NotFound: s.msgs[key] = None
                ch = await chan_for(key, s.kind)
                s.msgs[key] = await ch.send(MARK + s.kind, file=discord.File(io.BytesIO(raw), f"{s.kind}.json")); cloud["saved"] = int(time.time())
            except Exception as e:
                s.dirty.add(key); cloud["err"] = repr(e); print("Database save failed:", repr(e))

async def flusher():
    while True: await asyncio.sleep(30); await cloud_flush()

async def keepalive():  # pings our own public URL so Render's free plan doesn't put us to sleep
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url: return
    await asyncio.sleep(60)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as ses:
        while True:
            try: await ses.get(url.rstrip("/") + "/health")
            except Exception: pass
            await asyncio.sleep(600)

class Tree(app_commands.CommandTree):  # commands wait until the database has been loaded
    async def interaction_check(self, i):
        if not db_ready.is_set():
            try: await asyncio.wait_for(db_ready.wait(), 15)
            except asyncio.TimeoutError:
                await i.response.send_message("Starting up, try again in a moment 🌸", ephemeral=True); return False
        return True

DEFAULTS = {"welcome_channel": None, "welcome_msg": "Welcome {user} to {server}! 🌸",
            "goodbye_channel": None, "log_channel": None, "autorole": None,
            "raid_threshold": 8, "xp_enabled": True, "ai_enabled": True}

def gs(gid):
    s = SET.g(gid); [s.setdefault(k, v) for k, v in DEFAULTS.items()]; return s
def ud(gid, uid):
    u = USERS.g(gid).setdefault(str(uid), {}); [u.setdefault(k, v) for k, v in
        {"xp": 0, "coins": 0, "last_daily": 0, "warns": []}.items()]; return u
def emb(title, desc="", color=PINK):
    return discord.Embed(title=title, description=desc, color=color)

intents = discord.Intents.default(); intents.members = True; intents.message_content = True  # needed so "mika"/"mikasa" can trigger AI
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None, tree_cls=Tree)
NAME_RE = re.compile(r"\b(mikasa|mika)\b", re.I)
joins = defaultdict(lambda: deque(maxlen=50)); xp_cd = {}

async def log(guild, e):
    record(guild, e.title, e.description)
    ch = guild.get_channel(gs(guild.id)["log_channel"] or 0)
    if ch:
        try: await ch.send(embed=e)
        except discord.HTTPException: pass

def can_act(inter, target):
    return target != inter.user and target.top_role < inter.user.top_role and \
        target.top_role < inter.guild.me.top_role and target != inter.guild.owner

# ---------- events ----------
@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game("🌸 /help"))
    if not db_ready.is_set():
        for n in range(3):
            try: await db_load(); break
            except Exception as e: cloud["err"] = repr(e); print("Database load failed:", repr(e)); await asyncio.sleep(5 * (n + 1))
        else: print("FATAL: could not read the data server; exiting so Render restarts me."); os._exit(1)
        db_ready.set()
        if centre(): spawn(provision_all())   # new servers / renames since last run
    print(f"Ready as {bot.user} in {len(bot.guilds)} servers")

@bot.event
async def on_member_join(m):
    if not db_ready.is_set(): return
    s = gs(m.guild.id)
    if s["autorole"] and (r := m.guild.get_role(s["autorole"])) and r < m.guild.me.top_role:
        try: await m.add_roles(r, reason="Autorole")
        except discord.HTTPException: pass
    if (ch := m.guild.get_channel(s["welcome_channel"] or 0)):
        txt = s["welcome_msg"].replace("{user}", m.mention).replace("{server}", m.guild.name)
        try: await ch.send(txt[:1000], allowed_mentions=discord.AllowedMentions(users=[m]))
        except discord.HTTPException: pass
    q = joins[m.guild.id]; now = time.time(); q.append(now)  # defensive alert only
    if sum(1 for t in q if now - t < 10) >= s["raid_threshold"]:
        q.clear(); await log(m.guild, emb("🚨 Possible raid", f"{s['raid_threshold']}+ joins in 10s. Review members / enable verification.", 0xED4245))

@bot.event
async def on_member_remove(m):
    if not db_ready.is_set(): return
    if (ch := m.guild.get_channel(gs(m.guild.id)["goodbye_channel"] or 0)):
        try: await ch.send(f"Goodbye **{discord.utils.escape_markdown(m.display_name)}** 🌸")
        except discord.HTTPException: pass

@bot.event
async def on_guild_join(g):
    if db_ready.is_set() and centre(): spawn(provision_all())

@bot.event
async def on_guild_update(before, after):
    if before.name != after.name and db_ready.is_set() and centre(): spawn(provision_all())

@bot.event
async def on_message(msg):
    if msg.author.bot or not msg.guild or not db_ready.is_set(): return
    replied = msg.reference and isinstance(msg.reference.resolved, discord.Message) and msg.reference.resolved.author == bot.user
    if (bot.user in msg.mentions or replied or NAME_RE.search(msg.content or "")) and gs(msg.guild.id)["ai_enabled"] \
            and time.time() - ai_cd.get(msg.author.id, 0) >= 4:
        q = msg.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        if q:
            ai_cd[msg.author.id] = time.time()
            async with msg.channel.typing(): out = await ai_reply(msg.guild.id, msg.channel.id, q)
            if out: await msg.reply(out, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    if not gs(msg.guild.id)["xp_enabled"]: return
    k = (msg.guild.id, msg.author.id)
    if time.time() - xp_cd.get(k, 0) < 60: return
    xp_cd[k] = time.time(); u = ud(*k); old = u["xp"] // 100; u["xp"] += random.randint(5, 15)
    if u["xp"] // 100 > old:
        try: await msg.channel.send(f"✨ {msg.author.mention} reached level **{u['xp']//100}**!", delete_after=15,
                                    allowed_mentions=discord.AllowedMentions(users=[msg.author]))
        except discord.HTTPException: pass
    USERS.save(msg.guild.id)

# ---------- info / fun ----------
T = bot.tree
@T.command(description="Show commands")
async def help(i: discord.Interaction):
    await i.response.send_message(embed=emb("🌸 Mikasa 2.0", "**Mod:** kick ban unban timeout untimeout warn warnings purge slowmode lock unlock\n"
        "**Community:** rank leaderboard daily balance poll serverinfo whois\n**AI:** say mika / mikasa or @mention me • ask aiclear aistatus\n**Fun:** 8ball coinflip roll choose rps\nManage settings from the dashboard.\n**Owner:** /set data centre • /dbstatus"), ephemeral=True)
@T.command(description="Bot latency")
async def ping(i: discord.Interaction): await i.response.send_message(f"🏓 {round(bot.latency*1000)}ms")
@T.command(description="Server info")
@app_commands.guild_only()
async def serverinfo(i: discord.Interaction):
    g = i.guild; e = emb(g.name); e.add_field(name="Members", value=g.member_count); e.add_field(name="Channels", value=len(g.channels))
    e.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "D")); await i.response.send_message(embed=e)
@T.command(description="User info")
@app_commands.guild_only()
async def whois(i: discord.Interaction, member: discord.Member):
    e = emb(str(member)); e.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "D") if member.joined_at else "?")
    e.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "D")); e.set_thumbnail(url=member.display_avatar.url)
    await i.response.send_message(embed=e)
@T.command(name="8ball", description="Ask the 8ball")
async def eightball(i: discord.Interaction, question: str):
    await i.response.send_message(f"🎱 {random.choice(['Yes','No','Maybe','Definitely','Ask again later','Unlikely'])}")
@T.command(description="Flip a coin")
async def coinflip(i: discord.Interaction): await i.response.send_message(random.choice(["🪙 Heads", "🪙 Tails"]))
@T.command(description="Roll a die")
async def roll(i: discord.Interaction, sides: app_commands.Range[int, 2, 1000] = 6): await i.response.send_message(f"🎲 {random.randint(1, sides)}")
@T.command(description="Pick one option (comma separated)")
async def choose(i: discord.Interaction, options: str):
    o = [x.strip() for x in options.split(",") if x.strip()]
    await i.response.send_message(discord.utils.escape_mentions(random.choice(o)) if o else "Give me options!")
@T.command(description="Rock paper scissors")
async def rps(i: discord.Interaction, pick: str):
    p = pick.lower()
    if p not in ("rock", "paper", "scissors"): return await i.response.send_message("rock, paper or scissors", ephemeral=True)
    b = random.choice(["rock", "paper", "scissors"]); w = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    await i.response.send_message(f"I chose **{b}** — " + ("tie" if b == p else "you win! 🌸" if w[p] == b else "I win!"))
@T.command(description="Create a yes/no poll")
@app_commands.guild_only()
async def poll(i: discord.Interaction, question: app_commands.Range[str, 1, 200]):
    await i.response.send_message(embed=emb("📊 Poll", discord.utils.escape_mentions(question)))
    m = await i.original_response(); await m.add_reaction("👍"); await m.add_reaction("👎")

# ---------- levels / economy ----------
@T.command(description="Your level")
@app_commands.guild_only()
async def rank(i: discord.Interaction, member: discord.Member = None):
    m = member or i.user; u = ud(i.guild.id, m.id)
    await i.response.send_message(embed=emb(f"{m.display_name}", f"Level **{u['xp']//100}** • {u['xp']} XP • {u['coins']} 🌸"))
@T.command(description="Top members")
@app_commands.guild_only()
async def leaderboard(i: discord.Interaction):
    rows = sorted(((int(k), v.get("xp", 0)) for k, v in USERS.g(i.guild.id).items()), key=lambda x: -x[1])[:10]
    await i.response.send_message(embed=emb("🏆 Leaderboard", "\n".join(f"{n+1}. <@{u}> — {x} XP" for n, (u, x) in enumerate(rows)) or "No data yet"),
                                  allowed_mentions=discord.AllowedMentions.none())
@T.command(description="Daily coins")
@app_commands.guild_only()
async def daily(i: discord.Interaction):
    u = ud(i.guild.id, i.user.id)
    if time.time() - u["last_daily"] < 86400: return await i.response.send_message("Come back later 🌸", ephemeral=True)
    u["last_daily"] = time.time(); u["coins"] += 250; USERS.save(i.guild.id); await i.response.send_message("+250 🌸 collected!")
@T.command(description="Your coins")
@app_commands.guild_only()
async def balance(i: discord.Interaction): await i.response.send_message(f"{ud(i.guild.id, i.user.id)['coins']} 🌸", ephemeral=True)

# ---------- moderation ----------
def mod(**p): return lambda f: app_commands.guild_only()(app_commands.default_permissions(**p)(app_commands.checks.has_permissions(**p)(f)))
async def guard(i, m):
    if not can_act(i, m): await i.response.send_message("I can't act on that member (role hierarchy).", ephemeral=True); return False
    return True
@T.command(description="Kick a member")
@mod(kick_members=True)
async def kick(i: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    if await guard(i, member): await member.kick(reason=f"{i.user}: {reason}"); await i.response.send_message(f"👢 Kicked {member}"); await log(i.guild, emb("Kick", f"{member} by {i.user}\n{reason}"))
@T.command(description="Ban a member")
@mod(ban_members=True)
async def ban(i: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    if await guard(i, member): await member.ban(reason=f"{i.user}: {reason}"); await i.response.send_message(f"🔨 Banned {member}"); await log(i.guild, emb("Ban", f"{member} by {i.user}\n{reason}"))
@T.command(description="Unban by user ID")
@mod(ban_members=True)
async def unban(i: discord.Interaction, user_id: str):
    try: await i.guild.unban(discord.Object(int(user_id))); await i.response.send_message("Unbanned ✅")
    except (ValueError, discord.HTTPException): await i.response.send_message("Couldn't unban that ID.", ephemeral=True)
@T.command(description="Timeout a member")
@mod(moderate_members=True)
async def timeout(i: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason"):
    if await guard(i, member): await member.timeout(timedelta(minutes=minutes), reason=reason); await i.response.send_message(f"🔇 {member} timed out {minutes}m")
@T.command(description="Remove timeout")
@mod(moderate_members=True)
async def untimeout(i: discord.Interaction, member: discord.Member):
    if await guard(i, member): await member.timeout(None); await i.response.send_message(f"🔊 {member} untimed out")
@T.command(description="Warn a member")
@mod(moderate_members=True)
async def warn(i: discord.Interaction, member: discord.Member, reason: str):
    u = ud(i.guild.id, member.id); u["warns"].append({"by": i.user.id, "reason": reason[:200], "t": int(time.time())}); USERS.save(i.guild.id); record(i.guild, "Warn", f"{member} by {i.user}: {reason}")
    await i.response.send_message(f"⚠️ Warned {member} ({len(u['warns'])} total)")
@T.command(description="View warnings")
@mod(moderate_members=True)
async def warnings(i: discord.Interaction, member: discord.Member):
    w = ud(i.guild.id, member.id)["warns"]; await i.response.send_message("\n".join(f"{n+1}. {x['reason']}" for n, x in enumerate(w)) or "No warnings", ephemeral=True)
@T.command(description="Delete recent messages (max 100)")
@mod(manage_messages=True)
async def purge(i: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await i.response.defer(ephemeral=True); d = await i.channel.purge(limit=amount); await i.followup.send(f"Deleted {len(d)}")
@T.command(description="Set slowmode seconds")
@mod(manage_channels=True)
async def slowmode(i: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    await i.channel.edit(slowmode_delay=seconds); await i.response.send_message(f"Slowmode {seconds}s")
@T.command(description="Lock this channel")
@mod(manage_channels=True)
async def lock(i: discord.Interaction):
    await i.channel.set_permissions(i.guild.default_role, send_messages=False); await i.response.send_message("🔒 Locked")
@T.command(description="Unlock this channel")
@mod(manage_channels=True)
async def unlock(i: discord.Interaction):
    await i.channel.set_permissions(i.guild.default_role, send_messages=None); await i.response.send_message("🔓 Unlocked")


# ---------- AI chat ----------
AI_PROVIDERS = {  # name: (env key, url, default model)
    "groq": ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions", "openai/gpt-4o-mini"),
    "xai": ("XAI_API_KEY", "https://api.x.ai/v1/chat/completions", "grok-2-latest"),
    "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/messages", "claude-haiku-4-5"),
}
AI_SYSTEM = ("You are Mikasa, a friendly, cute anime-style Discord assistant. Keep replies under 1500 characters. "
             "Be kind and helpful. Refuse requests for harassment, raids, hacking, malware, or anything harmful.")
ai_cd = {}
def ai_hist(gid, cid): return AIMEM.g(gid).setdefault(str(cid), [])   # persisted in the server's #ai-memory channel

def ai_pick():
    want = os.getenv("MIKASA_AI_PROVIDER", "auto").lower()
    for n, (k, _, _) in AI_PROVIDERS.items():
        if want in ("auto", n) and os.getenv(k): return n
    return None

async def ai_reply(gid, channel_id, user_text):
    n = ai_pick()
    if not n: return None
    key, url, model = AI_PROVIDERS[n]; key = os.getenv(key); model = os.getenv("MIKASA_AI_MODEL") or model
    h = ai_hist(gid, channel_id); mine = {"role": "user", "content": user_text[:1500]}; h.append(mine)
    msgs = list(h)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as ses:
            if n == "anthropic":
                r = await ses.post(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                                   json={"model": model, "max_tokens": 600, "system": AI_SYSTEM, "messages": msgs})
                d = await r.json(); out = d["content"][0]["text"]
            else:
                r = await ses.post(url, headers={"Authorization": f"Bearer {key}"},
                                   json={"model": model, "max_tokens": 600, "messages": [{"role": "system", "content": AI_SYSTEM}] + msgs})
                d = await r.json(); out = d["choices"][0]["message"]["content"]
    except Exception:
        if mine in h: h.remove(mine)
        return "My brain glitched, try again in a moment 🌸"
    h.append({"role": "assistant", "content": out}); del h[:-8]; AIMEM.save(gid); return out[:1900]

@T.command(description="Chat with Mikasa AI")
@app_commands.guild_only()
async def ask(i: discord.Interaction, question: app_commands.Range[str, 1, 1000]):
    if not gs(i.guild.id)["ai_enabled"]: return await i.response.send_message("AI chat is off here.", ephemeral=True)
    if time.time() - ai_cd.get(i.user.id, 0) < 4: return await i.response.send_message("Slow down a little 🌸", ephemeral=True)
    ai_cd[i.user.id] = time.time(); await i.response.defer()
    out = await ai_reply(i.guild.id, i.channel_id, question)
    await i.followup.send(out or "AI isn't set up yet (no API key).", allowed_mentions=discord.AllowedMentions.none())
@T.command(description="Clear AI memory for this channel")
@mod(manage_messages=True)
async def aiclear(i: discord.Interaction): AIMEM.g(i.guild.id).pop(str(i.channel_id), None); AIMEM.save(i.guild.id); await i.response.send_message("Memory cleared ✨", ephemeral=True)
@T.command(description="AI status")
async def aistatus(i: discord.Interaction): await i.response.send_message(f"AI provider: **{ai_pick() or 'not configured'}**", ephemeral=True)

# /set data centre  (bot owner only)
setg = app_commands.Group(name="set", description="Bot owner settings", guild_only=True)
datag = app_commands.Group(name="data", description="Bot owner: data storage")
setg.add_command(datag); T.add_command(setg)

@datag.command(name="centre", description="Bot owner: make THIS server Mikasa's data centre")
@app_commands.describe(restore="Load the data already stored in this server instead of copying the current data into it")
async def set_data_centre(i: discord.Interaction, restore: bool = False):
    if not await bot.is_owner(i.user): return await i.response.send_message("Owner only.", ephemeral=True)
    await i.response.defer(ephemeral=True); g = i.guild
    snap = (cloud["gid"], dict(chans), [(s, dict(s.msgs), set(s.dirty), s.d) for s in Store.all])
    try:
        cloud["gid"] = g.id
        await adopt(await centre_scan(g), restore)
        await ensure(GKEY)
        await cloud_flush()
        await chans[(GKEY, META)].send(MARK + "meta")      # marker last = this server is now the newest data centre
        cloud["err"] = None; cloud["legacy"] = None
    except Exception as e:
        cloud["gid"] = snap[0]; chans.clear(); chans.update(snap[1])
        for s, msgs, dirty, d in snap[2]: s.msgs, s.dirty, s.d = msgs, dirty, d
        if isinstance(e, discord.Forbidden):
            return await i.followup.send("I need **Administrator** (or Manage Channels + Manage Roles) here to build the data centre.", ephemeral=True)
        return await i.followup.send(f"Failed: `{repr(e)[:300]}`", ephemeral=True)
    spawn(provision_all())
    await i.followup.send(f"✅ **{g.name}** is now my data centre. I'm building one category per server I'm in "
                          f"(settings, users, ai-memory, whitelists, logs) plus a **🌐 Global Data** category. This can take a while with many servers - "
                          "`/dbstatus` shows progress. Don't delete those channels, and only let people you trust see this server.")

@T.command(name="dbstatus", description="Bot owner: data centre status")
async def dbstatus(i: discord.Interaction):
    if not await bot.is_owner(i.user): return await i.response.send_message("Owner only.", ephemeral=True)
    cg = centre(); st = f"✅ **{cg.name}** ({len(chans)} channels)" if cg else "❌ none set - run `/set data centre` in your data server"
    pr = f"\nCategories: {cloud['prov'][0]}/{cloud['prov'][1]} servers" if cloud["prov"] else ""
    pend = sum(len(s.dirty) for s in Store.all); pend = f"\nUnsaved changes: {pend}" if pend else ""
    last = f"\nLast save: <t:{cloud['saved']}:R>" if cloud["saved"] else ""
    old = f"\n⚠️ Old-format data from **{cloud['legacy']}** is loaded in memory only - run `/set data centre` to migrate it." if cloud["legacy"] and not cg else ""
    err = f"\nLast error: `{cloud['err'][:300]}`" if cloud["err"] else ""
    await i.response.send_message(f"**Data centre:** {st}{pr}{pend}{last}{old}{err}", ephemeral=True)

@T.error
async def on_err(i: discord.Interaction, e):
    if isinstance(e, app_commands.CheckFailure) and not isinstance(e, app_commands.MissingPermissions): return
    m = "You lack permission." if isinstance(e, app_commands.MissingPermissions) else "Something went wrong."
    send = i.followup.send if i.response.is_done() else i.response.send_message
    try: await send(m, ephemeral=True)
    except discord.HTTPException: pass

# ---------- dashboard ----------
def sign(ts): return hmac.new(SECRET, str(ts).encode(), hashlib.sha256).hexdigest()
def authed(r):
    try: ts, sig = r.cookies.get("s", "").split(".")
    except ValueError: return False
    return ts.isdigit() and hmac.compare_digest(sig, sign(ts)) and time.time() - int(ts) < 43200
fails = defaultdict(list)

@web.middleware
async def guard_mw(r, h):
    if r.path.startswith("/api/") and r.path != "/api/login":
        if not authed(r): return web.json_response({"error": "auth"}, status=401)
        if not db_ready.is_set(): return web.json_response({"error": "starting"}, status=503)
        if r.method == "POST" and r.headers.get("X-Requested-With") != "mikasa": return web.json_response({"error": "csrf"}, status=403)
    resp = await h(r)
    resp.headers.update({"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                         "Content-Security-Policy": "default-src 'self'; img-src 'self' data: https://cdn.discordapp.com https://*.discordapp.com; "
                         "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'"})
    return resp

async def login(r):
    ip = r.headers.get("X-Forwarded-For", r.remote or "").split(",")[0].strip(); now = time.time()
    fails[ip] = [t for t in fails[ip] if now - t < 600]
    if len(fails[ip]) >= 5: return web.json_response({"error": "locked"}, status=429)
    try: pw = (await r.json()).get("password", "")
    except Exception: pw = ""
    if not DASH_PASS or not hmac.compare_digest(str(pw).encode(), DASH_PASS.encode()):
        fails[ip].append(now); return web.json_response({"error": "bad"}, status=401)
    ts = int(now); resp = web.json_response({"ok": True})
    resp.set_cookie("s", f"{ts}.{sign(ts)}", httponly=True, samesite="Strict", secure=r.scheme == "https" or r.headers.get("X-Forwarded-Proto") == "https", max_age=43200)
    return resp
async def logout(r): resp = web.json_response({"ok": True}); resp.del_cookie("s"); return resp
async def stats(r):
    return web.json_response({"bot": str(bot.user), "latency": round(bot.latency * 1000), "servers": len(bot.guilds),
                              "members": sum(g.member_count or 0 for g in bot.guilds), "commands": len(T.get_commands())})
async def guilds(r): return web.json_response([{"id": str(g.id), "name": g.name, "members": g.member_count, "icon": g.icon.url if g.icon else None} for g in bot.guilds])
def gget(r):
    try: return bot.get_guild(int(r.match_info["gid"]))
    except ValueError: return None
async def guild_get(r):
    g = gget(r)
    if not g: return web.json_response({"error": "nf"}, status=404)
    return web.json_response({"settings": {k: (str(v) if k.endswith(("channel", "role")) or k == "autorole" else v) for k, v in gs(g.id).items() if v is not None},
        "channels": [{"id": str(c.id), "name": c.name} for c in g.text_channels], "roles": [{"id": str(x.id), "name": x.name} for x in g.roles if not x.managed and x != g.default_role]})
async def guild_set(r):
    g = gget(r)
    if not g: return web.json_response({"error": "nf"}, status=404)
    try: b = await r.json()
    except Exception: return web.json_response({"error": "bad"}, status=400)
    s = gs(g.id)
    for k in ("welcome_channel", "goodbye_channel", "log_channel"):
        if k in b: v = int(b[k]) if str(b[k]).isdigit() else None; s[k] = v if v and g.get_channel(v) else None
    if "autorole" in b:
        v = int(b["autorole"]) if str(b["autorole"]).isdigit() else None; s["autorole"] = v if v and g.get_role(v) else None
    if "welcome_msg" in b and isinstance(b["welcome_msg"], str): s["welcome_msg"] = b["welcome_msg"][:500]
    try:
        if "raid_threshold" in b: s["raid_threshold"] = max(3, min(50, int(b["raid_threshold"])))
    except (TypeError, ValueError): return web.json_response({"error": "bad"}, status=400)
    if "xp_enabled" in b: s["xp_enabled"] = bool(b["xp_enabled"])
    if "ai_enabled" in b: s["ai_enabled"] = bool(b["ai_enabled"])
    SET.save(g.id); return web.json_response({"ok": True})
async def index(r):
    f = BASE / "dashboard.html"
    if f.exists(): return web.FileResponse(f)
    return web.Response(status=500, content_type="text/html", text="<h2>Mikasa dashboard</h2><p><code>dashboard.html</code> is missing from the deployment. "
                        "Put it in the same folder as <code>Mikasa.py</code>, commit and redeploy.</p>")
async def health(r): return web.Response(text="ok")

async def main():
    if not TOKEN: raise SystemExit("TOKEN env var missing")
    if not DASH_PASS: print("WARNING: DASHBOARD_PASSWORD is not set - nobody can log in to the dashboard.")
    if not (BASE / "dashboard.html").exists(): print("WARNING: dashboard.html not found next to Mikasa.py - the dashboard page will not load.")
    app = web.Application(middlewares=[guard_mw])
    app.add_routes([web.get("/", index), web.get("/health", health), web.post("/api/login", login), web.post("/api/logout", logout),
        web.get("/api/stats", stats), web.get("/api/guilds", guilds), web.get("/api/guild/{gid}", guild_get), web.post("/api/guild/{gid}", guild_set)])
    runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner, "0.0.0.0", PORT).start()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    async with bot:
        try: await bot.login(TOKEN)
        except discord.LoginFailure: raise SystemExit("Invalid TOKEN - reset it in the Discord Developer Portal and update the Render env var.")
        await T.sync()
        conn = asyncio.create_task(bot.connect()); bg = [asyncio.create_task(flusher()), asyncio.create_task(keepalive())]
        waiter = asyncio.create_task(stop.wait())
        await asyncio.wait([conn, waiter], return_when=asyncio.FIRST_COMPLETED)
        if db_ready.is_set(): await cloud_flush()
        for t in bg + [waiter]: t.cancel()
        if conn.done() and not conn.cancelled() and conn.exception():
            e = conn.exception()
            if isinstance(e, discord.PrivilegedIntentsRequired):
                raise SystemExit("Enable BOTH 'Server Members Intent' and 'Message Content Intent' in the Developer Portal > Bot.")
            raise e
        conn.cancel()

if __name__ == "__main__": asyncio.run(main())
