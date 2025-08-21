import os, asyncio, aiosqlite, datetime as dt
from difflib import SequenceMatcher
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
load_dotenv()


# ======= CONFIG (FILL THESE) =======
GUILD_ID            = 1407504761263095989         # your server ID
CHANNEL_CHECKINS    = 1407505632457658399          # #daily-check-in
CHANNEL_LEADERBOARD = 1407505097503543397          # #streak-leaderboard
CHANNEL_LOGS        = 1407743523817787522          # #streak-logs
CHANNEL_WEEKLY      = 1407506115952119818          # #weekly-progress

ROLE_VALIDATOR      = 1407743740885602337          # Validator role ID
ROLE_SENIOR_VALID   = 1407743878551044207          # optional; else set same as ROLE_VALIDATOR
''
VALIDATION_QUORUM   = 1                           # e.g., 3 validators for approval (scale to 5 later)
MIN_REF_CHARS       = 150                         # minimum reflection length
MIN_HOURS           = 20                          # cooldown lower bound
MAX_HOURS           = 28                          # cooldown upper bound
SIMILARITY_BLOCK    = 0.90                        # >= 0.90 similarity to last entry -> flag/reject

LEADERBOARD_SIZE    = 10                          # top N on LB

BOT_TOKEN = os.getenv("BOT_TOKEN")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True
INTENTS.reactions = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree

DB_PATH = "/data/getbilldbot-volume"
LEADERBOARD_MESSAGE_ID = None   # populated after first run; bot will pin it
TEST_GUILD = discord.Object(id=GUILD_ID)

# ======= DB init =======
CREATE_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  current_streak INTEGER NOT NULL DEFAULT 0,
  longest_streak INTEGER NOT NULL DEFAULT 0,
  last_checkin_at TEXT,
  frozen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS checkins(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  message_id INTEGER,
  channel_id INTEGER,
  created_at TEXT NOT NULL,
  day_reported INTEGER,
  reflection TEXT NOT NULL,
  proof_url TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected|expired
  validators TEXT DEFAULT '[]',
  similar_flag INTEGER NOT NULL DEFAULT 0,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

async def init_db():
    async with aiosqlite.connect("streaks.db") as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

# 2) Helper: execute (uses an existing db connection)
async def db_exec(db, query, *params):
    await db.execute(query, params)
    await db.commit()

# 3) Helper: fetch one row
async def db_fetchone(db, query, *params):
    cur = await db.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return row


# ======= Utilities =======
def now_utc():
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

def hours_since(ts_iso: str|None):
    if not ts_iso: return 10**6
    t = dt.datetime.fromisoformat(ts_iso)
    return (now_utc() - t).total_seconds()/3600.0

def sim(a,b):
    return SequenceMatcher(a=a.strip(), b=b.strip()).ratio()

def is_validator(member: discord.Member) -> bool:
    return any(r.id in (ROLE_VALIDATOR, ROLE_SENIOR_VALID) for r in member.roles)

def weight_for(member: discord.Member) -> float:
    return 1.5 if any(r.id == ROLE_SENIOR_VALID for r in member.roles) else 1.0

async def ensure_leaderboard_message(guild):
    # Make sure schema exists
    await init_db()

    async with aiosqlite.connect("streaks.db") as db:
        # Try to read existing message id
        row = await db_fetchone(db, "SELECT value FROM meta WHERE key='lb_msg_id'")
        channel = guild.get_channel(CHANNEL_LEADERBOARD)

        if row:
            try:
                msg_id = int(row[0])
                msg = await channel.fetch_message(msg_id)
                return msg  # already have it
            except Exception:
                # message was deleted or invalid; we'll create a new one
                pass

        # Create a fresh leaderboard placeholder
        msg = await channel.send("🏆 Leaderboard will appear here shortly…")
        await db_exec(db,
            "INSERT OR REPLACE INTO meta(key,value) VALUES('lb_msg_id', ?)",
            str(msg.id)
        )
        return msg

async def update_leaderboard(guild: discord.Guild):
    chan = guild.get_channel(CHANNEL_LEADERBOARD)
    if not chan: return
    msg = await ensure_leaderboard_message(guild)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT user_id, current_streak, longest_streak, frozen
            FROM users
            WHERE frozen=0
            ORDER BY current_streak DESC, longest_streak DESC
            LIMIT ?""", (LEADERBOARD_SIZE,))
        rows = await cur.fetchall()
    lines = ["**🏆 Validated Streak Leaderboard**"]
    if not rows:
        lines.append("_No validated streaks yet._")
    else:
        for i,(uid,st,longest,frozen) in enumerate(rows, start=1):
            lines.append(f"{i}. <@{uid}> — **{st}** days (best: {longest})")
    text = "\n".join(lines)
    try:
        await msg.edit(content=text)
    except:
        await chan.send(text)

async def post_log(guild: discord.Guild, content: str):
    chan = guild.get_channel(CHANNEL_LOGS)
    if chan:
        await chan.send(content)

# ======= Modal =======
class CheckinModal(discord.ui.Modal, title="Daily Check-in"):
    day = discord.ui.TextInput(label="Day number (e.g., 7)", required=True, max_length=6)
    reflection = discord.ui.TextInput(
        label=f"Reflection (min {MIN_REF_CHARS} chars)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1800,
        placeholder="What was hard? How did you handle cravings? What helped?"
    )
    proof = discord.ui.TextInput(label="Proof URL (optional)", required=False, max_length=300, placeholder="Image/Note/Audio link (optional)")

    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        user = self.member
        # basic validation
        try:
            day_num = int(str(self.day.value).strip().replace("Day","").strip())
            if day_num < 0 or day_num > 10000:
                raise ValueError
        except:
            return await interaction.followup.send("❌ Day must be a positive integer.", ephemeral=True)

        if len(self.reflection.value.strip()) < MIN_REF_CHARS:
            return await interaction.followup.send(f"❌ Reflection must be at least {MIN_REF_CHARS} characters.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(CREATE_SQL)
            # cooldown
            cur = await db.execute("SELECT last_checkin_at FROM users WHERE user_id=?", (user.id,))
            row = await cur.fetchone()
            last_iso = row[0] if row else None
            hrs = hours_since(last_iso)
            if hrs < MIN_HOURS:
                return await interaction.followup.send(f"⏳ Too soon. Wait {MIN_HOURS-hrs:.1f} more hours.", ephemeral=True)
            if hrs > MAX_HOURS and last_iso is not None:
                # late: will still allow, but mark as late (could expire w/o quorum)
                pass

            # similarity check
            cur = await db.execute("SELECT reflection FROM checkins WHERE user_id=? AND status IN ('approved','pending') ORDER BY id DESC LIMIT 1", (user.id,))
            prev = await cur.fetchone()
            similar = 0
            if prev and prev[0]:
                if sim(prev[0], self.reflection.value) >= SIMILARITY_BLOCK:
                    similar = 1

            # create pending record
            now = now_utc().isoformat()
            await db.execute("""
              INSERT INTO checkins(user_id, created_at, day_reported, reflection, proof_url, status, similar_flag)
              VALUES(?,?,?,?,?, 'pending', ?)""",
              (user.id, now, day_num, self.reflection.value.strip(), (self.proof.value or "").strip(), similar))
            await db.commit()
            cur = await db.execute("SELECT last_insert_rowid()")
            chk_id = (await cur.fetchone())[0]

        # post pending card
        chan = guild.get_channel(CHANNEL_CHECKINS)
        embed = discord.Embed(title=f"Pending Check-in • {user.display_name}",
                              description=f"**Day {day_num}**\n\n{self.reflection.value.strip()[:1400]}",
                              color=discord.Color.orange())
        if self.proof.value:
            embed.add_field(name="Proof", value=self.proof.value[:300], inline=False)
        embed.set_footer(text=f"ID: {chk_id} • React ✅ (Validators) to validate")
        msg = await chan.send(content=user.mention, embed=embed)
        await msg.add_reaction("✅")

        # store message id
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE checkins SET message_id=?, channel_id=? WHERE id=?", (msg.id, chan.id, chk_id))
            await db.commit()

        flag_txt = " (⚠️ similar to last entry)" if similar else ""
        await interaction.followup.send(f"✅ Submitted! Your check-in is pending validator approval.{flag_txt}", ephemeral=True)
        await post_log(guild, f"📝 New check-in pending: <@{user.id}> Day {day_num}{flag_txt} (id {chk_id})")

# ======= Slash Commands =======
@tree.command(name="checkin", description="Submit your daily check-in")
# @app_commands.guilds(TEST_GUILD)  # for testing; remove in production
async def checkin_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(CheckinModal(interaction.user))

@tree.command(name="streak", description="View your current streak")
# @app_commands.guilds(TEST_GUILD)  # for testing; remove in production
async def streak_view(interaction: discord.Interaction, user: discord.Member|None=None):
    user = user or interaction.user
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT current_streak, longest_streak, last_checkin_at, frozen FROM users WHERE user_id=?", (user.id,))
        row = await cur.fetchone()
    if not row:
        return await interaction.response.send_message(f"{user.mention} has no streak yet.", ephemeral=True)
    st, longest, last, frozen = row
    fr = " (❄️ frozen)" if frozen else ""
    when = f" • last check-in: {last}" if last else ""
    await interaction.response.send_message(f"**{user.display_name}** — current: **{st}**, best: **{longest}**{fr}{when}", ephemeral=True)

# --- Admin group ---
admin = app_commands.Group(name="admin", description="Admin streak controls")

@admin.command(name="set")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_set(inter: discord.Interaction, user: discord.Member, value: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(user_id,current_streak,longest_streak,last_checkin_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET current_streak=excluded.current_streak,
                                              longest_streak=MAX(users.longest_streak, excluded.current_streak)
        """, (user.id, value, value, now_utc().isoformat()))
        await db.commit()
    await update_leaderboard(inter.guild)
    await inter.response.send_message(f"Set {user.mention} streak to {value}.", ephemeral=True)
    await post_log(inter.guild, f"🛠️ Admin set {user.mention} to {value} by {inter.user.mention}")

@admin.command(name="add")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_add(inter: discord.Interaction, user: discord.Member, delta: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT current_streak,longest_streak FROM users WHERE user_id=?", (user.id,))
        row = await cur.fetchone()
        st = row[0] if row else 0
        longest = row[1] if row else 0
        st += delta
        longest = max(longest, st)
        await db.execute("""
            INSERT INTO users(user_id,current_streak,longest_streak,last_checkin_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET current_streak=?,
                                              longest_streak=?,
                                              last_checkin_at=?
        """, (user.id, st, longest, now_utc().isoformat(), st, longest, now_utc().isoformat()))
        await db.commit()
    await update_leaderboard(inter.guild)
    await inter.response.send_message(f"Added {delta} → {user.mention} now {st}.", ephemeral=True)
    await post_log(inter.guild, f"🛠️ Admin add {delta} for {user.mention} by {inter.user.mention}")

@admin.command(name="reset")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_reset(inter: discord.Interaction, user: discord.Member):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users(user_id,current_streak,longest_streak,last_checkin_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET current_streak=0
        """, (user.id,0,0, now_utc().isoformat()))
        await db.commit()
    await update_leaderboard(inter.guild)
    await inter.response.send_message(f"Reset {user.mention}.", ephemeral=True)
    await post_log(inter.guild, f"⛔ Admin reset {user.mention} by {inter.user.mention}")

@admin.command(name="freeze")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_freeze(inter: discord.Interaction, user: discord.Member, frozen: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users(user_id,frozen) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET frozen=?",
                         (user.id, 1 if frozen else 0, 1 if frozen else 0))
        await db.commit()
    await update_leaderboard(inter.guild)
    await inter.response.send_message(f"{'Froze' if frozen else 'Unfroze'} {user.mention}.", ephemeral=True)

@admin.command(name="history")
@app_commands.checks.has_permissions(manage_guild=True)
async def admin_history(inter: discord.Interaction, user: discord.Member, limit: int=5):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
          SELECT id,created_at,day_reported,status,similar_flag
          FROM checkins WHERE user_id=?
          ORDER BY id DESC LIMIT ?""", (user.id, limit))
        rows = await cur.fetchall()
    if not rows:
        return await inter.response.send_message("No history.", ephemeral=True)
    lines = [f"Last {len(rows)} check-ins for {user.display_name}:"]
    for cid,ts,day,status,simf in rows:
        tag = "⚠️" if simf else ""
        lines.append(f"• #{cid} — Day {day} — {status} {tag} — {ts}")
    await inter.response.send_message("\n".join(lines), ephemeral=True)

tree.add_command(admin)

# ======= Reaction listener for quorum =======
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != "✅": return
    if payload.channel_id != CHANNEL_CHECKINS: return
    guild = bot.get_guild(GUILD_ID)
    member = guild.get_member(payload.user_id)
    if not member or member.bot: return
    if not is_validator(member): return

    chan = guild.get_channel(payload.channel_id)
    try:
        msg = await chan.fetch_message(payload.message_id)
    except:
        return

    # fetch checkin
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, user_id, status FROM checkins WHERE message_id=?", (msg.id,))
        row = await cur.fetchone()
        if not row: return
        chk_id, target_uid, status = row
        if status != "pending": return

        # count validator reactions (weighted)
        validators = set()
        weight_sum = 0.0
        for reaction in msg.reactions:
            if str(reaction.emoji) == "✅":
                async for user in reaction.users():
                    if user.bot: continue
                    m = guild.get_member(user.id)
                    if m and is_validator(m):
                        validators.add(m.id)
                        weight_sum += weight_for(m)

        if weight_sum >= VALIDATION_QUORUM:
            # APPROVE
            # increment streak with cooldown enforcement
            # load user
            cur2 = await db.execute("SELECT current_streak,longest_streak,last_checkin_at FROM users WHERE user_id=?", (target_uid,))
            u = await cur2.fetchone()
            last_iso = u[2] if u else None
            hrs = hours_since(last_iso)
            if u is None:
                current,longest = 0,0
            else:
                current,longest = u[0],u[1]

            # If too soon (< MIN_HOURS), mark rejected to avoid gaming via reactions
            if last_iso and hrs < MIN_HOURS:
                await db.execute("UPDATE checkins SET status='rejected', reason='cooldown' WHERE id=?", (chk_id,))
                await db.commit()
                await post_log(guild, f"❌ Rejected (cooldown) for <@{target_uid}> on #{chk_id}")
                return

            current += 1
            longest = max(longest, current)
            nowiso = now_utc().isoformat()
            await db.execute("""
                INSERT INTO users(user_id,current_streak,longest_streak,last_checkin_at)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET current_streak=?,
                                                  longest_streak=?,
                                                  last_checkin_at=?
            """, (target_uid, current, longest, nowiso, current, longest, nowiso))
            await db.execute("UPDATE checkins SET status='approved' WHERE id=?", (chk_id,))
            await db.commit()

            try:
                user = guild.get_member(target_uid)
                if user:
                    await user.send(f"✅ Your check-in was approved. New streak: **{current}** days.")
            except: pass

            embed = msg.embeds[0]
            new_embed = discord.Embed(
                title=embed.title,
                description=embed.description,
                color=discord.Color.green()
            )
            for field in embed.fields:
                new_embed.add_field(name=field.name, value=field.value, inline=field.inline)

            if embed.footer:
                new_embed.set_footer(text=embed.footer.text, icon_url=embed.footer.icon_url)

            if embed.author:
                new_embed.set_author(name=embed.author.name, icon_url=embed.author.icon_url)

            await msg.edit(embed=new_embed)

            await post_log(guild, f"✅ Approved by quorum: <@{target_uid}> → {current} days (check-in #{chk_id})")
            await update_leaderboard(guild)

# ======= Background task: expire pending after 24h & freeze weekly =======
async def maintenance_loop():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    while not bot.is_closed():
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # expire >24h pendings
                cutoff = (now_utc() - dt.timedelta(hours=24)).isoformat()
                cur = await db.execute("SELECT id, user_id FROM checkins WHERE status='pending' AND created_at<?", (cutoff,))
                rows = await cur.fetchall()
                for cid, uid in rows:
                    await db.execute("UPDATE checkins SET status='expired' WHERE id=?", (cid,))
                    await post_log(guild, f"⏳ Expired check-in #{cid} for <@{uid}> (no quorum)")
                await db.commit()

                # weekly freeze check
                # if user has validated streak but no message in weekly channel in last 7 days -> frozen=1
                # This is a lightweight heuristic using Discord search via audit is not available here.
                # Instead, we keep frozen manual for now OR plug in via admin command / external task.
                # (You can turn off if not needed.)
        except Exception as e:
            try:
                await post_log(guild, f"⚠️ maintenance error: {e}")
            except: pass
        await asyncio.sleep(1800)  # run every 30 minutes

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    await bot.wait_until_ready()

    # DB init
    await init_db()

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print(f"❌ Bot is not in guild {GUILD_ID}. Re-invite with applications.commands scope.")
        return

    # Ensure LB
    await ensure_leaderboard_message(guild)
    await update_leaderboard(guild)

    # ✅ Correct method name here
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Synced {len(synced)} slash command(s) to {guild.name} ({guild.id})")

    bot.loop.create_task(maintenance_loop())


# Run
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var.")
    bot.run(BOT_TOKEN)

# ======= End of Main.py =======
# python c:/Users/Thanh/OneDrive/Documents/Coding/getbilld/Main.py