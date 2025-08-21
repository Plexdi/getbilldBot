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

DB_PATH = "/data/streaks.db"
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
    async with aiosqlite.connect(DB_PATH) as db:
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
async def approve_checkin(checkin_id: int, validator_id: int, guild: discord.Guild):
    async with aiosqlite.connect(DB_PATH) as db:
        # fetch the checkin
        cur = await db.execute("SELECT user_id, created_at FROM checkins WHERE id=?", (checkin_id,))
        row = await cur.fetchone()
        if not row:
            return None
        user_id, created_at = row

        # mark as approved
        await db.execute("UPDATE checkins SET status='approved' WHERE id=?", (checkin_id,))

        # streak logic
        cur = await db.execute("SELECT last_checkin_at, streak_count FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        last_iso, streak = row if row else (None, 0)

        hrs = hours_since(last_iso)
        if hrs <= MAX_HOURS:  # continued streak
            streak += 1
        else:
            streak = 1  # reset streak

        now = now_utc().isoformat()
        await db.execute("""
            INSERT INTO users(user_id, last_checkin_at, streak_count)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET last_checkin_at=excluded.last_checkin_at,
                                              streak_count=excluded.streak_count
        """, (user_id, now, streak))

        await db.commit()

    return user_id, streak


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

    async with aiosqlite.connect(DB_PATH) as db:
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

@tree.command(name="leaderboard", description="Show top streaks")
async def leaderboard_cmd(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT user_id, streak_count 
            FROM users 
            ORDER BY streak_count DESC 
            LIMIT 10
        """)
        rows = await cur.fetchall()

    if not rows:
        return await interaction.response.send_message("No check-ins yet.", ephemeral=True)

    desc = "\n".join(
        f"**{i+1}.** <@{uid}> — 🔥 {streak} days"
        for i, (uid, streak) in enumerate(rows)
    )
    embed = discord.Embed(title="🏆 Leaderboard", description=desc, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)

@tree.command(name="dbinfo", description="Show quick DB stats")
@app_commands.checks.has_permissions(manage_guild=True)
async def dbinfo(inter: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cu1 = await db.execute("SELECT COUNT(*) FROM users")
        n_users = (await cu1.fetchone())[0]
        cu2 = await db.execute("SELECT COUNT(*) FROM checkins WHERE status='pending'")
        n_pending = (await cu2.fetchone())[0]
        cu3 = await db.execute("SELECT COUNT(*) FROM checkins WHERE status='approved'")
        n_approved = (await cu3.fetchone())[0]
    await inter.response.send_message(
        f"**DB:** `{DB_PATH}`\nUsers: **{n_users}**\nCheckins: **{n_pending} pending**, **{n_approved} approved**",
        ephemeral=True
    )


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
    # --- quick debug: comment in if needed ---
    # print(f"[raw_react] emoji={payload.emoji} ch={payload.channel_id} msg={payload.message_id} user={payload.user_id}")

    # 1) Filter for the right emoji and channel
    if str(payload.emoji) != "✅":
        return
    if payload.channel_id != CHANNEL_CHECKINS:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    # 2) Resolve the reacting member robustly
    member = getattr(payload, "member", None)
    if member is None:
        member = guild.get_member(payload.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except Exception:
            return

    # ignore bots and non-validators
    if member.bot:
        return
    if not is_validator(member):
        return

    # 3) Fetch the message we reacted to
    chan = guild.get_channel(payload.channel_id)
    if not chan:
        return
    try:
        msg = await chan.fetch_message(payload.message_id)
    except Exception:
        return

    # 4) Look up the pending check-in row for this message
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, status FROM checkins WHERE message_id=?",
            (msg.id,)
        )
        row = await cur.fetchone()

    if not row:
        return

    chk_id, target_uid, status = row
    if status != "pending":
        return

    # 5) Count validator reactions (weighted), including this new reaction
    validators = set()
    weight_sum = 0.0
    for reaction in msg.reactions:
        # Some emojis come through as PartialEmoji; compare by string
        if str(reaction.emoji) == "✅":
            async for u in reaction.users():
                if u.bot:
                    continue
                m = guild.get_member(u.id) or (await guild.fetch_member(u.id))
                if m and is_validator(m):
                    if m.id not in validators:
                        validators.add(m.id)
                        weight_sum += weight_for(m)

    # 6) If quorum not reached yet, stop here
    if weight_sum < VALIDATION_QUORUM:
        return

    # 7) Quorum reached: APPROVE the check-in and update streaks (schema = current_streak/longest_streak)
    async with aiosqlite.connect(DB_PATH) as db:
        # Double-check still pending (avoid race)
        cur = await db.execute("SELECT status FROM checkins WHERE id=?", (chk_id,))
        st_row = await cur.fetchone()
        if not st_row or st_row[0] != "pending":
            return

        # Cooldown enforcement
        cur = await db.execute(
            "SELECT current_streak,longest_streak,last_checkin_at FROM users WHERE user_id=?",
            (target_uid,)
        )
        u = await cur.fetchone()
        last_iso = u[2] if u else None
        hrs = hours_since(last_iso)
        if last_iso and hrs < MIN_HOURS:
            await db.execute(
                "UPDATE checkins SET status='rejected', reason='cooldown' WHERE id=?",
                (chk_id,)
            )
            await db.commit()
            await post_log(guild, f"❌ Rejected (cooldown) for <@{target_uid}> on #{chk_id}")
            return

        current = (u[0] if u else 0) + 1
        longest = max(u[1], current) if u else current
        nowiso = now_utc().isoformat()

        # Write user streak + approve the check-in
        await db.execute("""
            INSERT INTO users(user_id,current_streak,longest_streak,last_checkin_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET current_streak=?,
                                              longest_streak=?,
                                              last_checkin_at=?
        """, (target_uid, current, longest, nowiso, current, longest, nowiso))

        await db.execute("UPDATE checkins SET status='approved' WHERE id=?", (chk_id,))
        await db.commit()

    # 8) DM user (best effort), update embed color, log, refresh leaderboard
    try:
        user = guild.get_member(target_uid) or await guild.fetch_member(target_uid)
        if user:
            try:
                await user.send(f"✅ Your check-in was approved. New streak: **{current}** days.")
            except Exception:
                pass
    except Exception:
        pass

    # Rebuild the embed in green while preserving fields/footer/author
    try:
        old = msg.embeds[0]
        new_e = discord.Embed(title=old.title, description=old.description, color=discord.Color.green())
        for f in old.fields:
            new_e.add_field(name=f.name, value=f.value, inline=f.inline)
        if old.footer:
            new_e.set_footer(text=old.footer.text, icon_url=getattr(old.footer, "icon_url", None))
        if old.author:
            new_e.set_author(name=old.author.name, icon_url=getattr(old.author, "icon_url", None))
        await msg.edit(embed=new_e)
    except Exception:
        pass

    await msg.channel.send(f"✅ Check-in approved for <@{target_uid}>! Current streak: **{current}** days")
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

# ================== Daily Motivation ==================
# Config: paste your messages here ↓↓↓
QUOTES: list[str] = [
  "Believe in yourself, and you are halfway there.",
  "Success is not final, failure is not fatal: it is the courage to continue that counts.",
  "Dream big and dare to fail.",
  "Don’t watch the clock; do what it does. Keep going.",
  "Act as if what you do makes a difference. It does.",
  "Your limitation—it’s only your imagination.",
  "Push yourself, because no one else is going to do it for you.",
  "Great things never come from comfort zones.",
  "Dream it. Wish it. Do it.",
  "Success doesn’t just find you. You have to go out and get it.",
  "The harder you work for something, the greater you’ll feel when you achieve it.",
  "Don’t stop when you’re tired. Stop when you’re done.",
  "Wake up with determination. Go to bed with satisfaction.",
  "Do something today that your future self will thank you for.",
  "Little things make big days.",
  "It’s going to be hard, but hard does not mean impossible.",
  "Don’t wait for opportunity. Create it.",
  "Sometimes we’re tested not to show our weaknesses, but to discover our strengths.",
  "The key to success is to focus on goals, not obstacles.",
  "Dream it. Believe it. Build it.",
  "Difficult roads often lead to beautiful destinations.",
  "Opportunities don’t happen. You create them.",
  "The way to get started is to quit talking and begin doing.",
  "Don’t limit your challenges. Challenge your limits.",
  "Believe you can and you’re halfway there.",
  "The secret to getting ahead is getting started.",
  "Everything you’ve ever wanted is on the other side of fear.",
  "Doubt kills more dreams than failure ever will.",
  "Failure is not the opposite of success; it’s part of success.",
  "Work hard in silence, let your success be the noise.",
  "Don’t let yesterday take up too much of today.",
  "Keep your eyes on the stars, and your feet on the ground.",
  "It always seems impossible until it’s done.",
  "Perseverance is not a long race; it is many short races one after the other.",
  "Strength grows in the moments you think you can’t go on but you keep going anyway.",
  "Don’t wish it were easier. Wish you were better.",
  "Your passion is waiting for your courage to catch up.",
  "If you can dream it, you can do it.",
  "Success usually comes to those who are too busy to be looking for it.",
  "The future depends on what you do today.",
  "Hard work beats talent when talent doesn’t work hard.",
  "The only way to achieve the impossible is to believe it is possible.",
  "Start where you are. Use what you have. Do what you can.",
  "Success is the sum of small efforts repeated day in and day out.",
  "Fall seven times and stand up eight.",
  "Winners are not afraid of losing. But losers are.",
  "When you feel like quitting, remember why you started.",
  "Don’t count the days, make the days count.",
  "The man who moves a mountain begins by carrying away small stones.",
  "Success doesn’t come from what you do occasionally, it comes from what you do consistently.",
  "You don’t have to be great to start, but you have to start to be great.",
  "Don’t give up. Great things take time.",
  "Discipline is the bridge between goals and accomplishment.",
  "If it doesn’t challenge you, it won’t change you.",
  "Your only limit is you.",
  "Stay positive, work hard, make it happen.",
  "Success is walking from failure to failure with no loss of enthusiasm.",
  "Big journeys begin with small steps.",
  "Hustle in silence and let your success make the noise.",
  "Don’t fear failure. Fear being in the exact same place next year as you are today.",
  "Work hard, stay humble.",
  "Stop doubting yourself, work hard, and make it happen.",
  "Great things never come from comfort zones.",
  "Push harder than yesterday if you want a different tomorrow.",
  "Doubt kills more dreams than failure ever will.",
  "You are stronger than you think.",
  "What seems hard now will one day be your warm-up.",
  "Stay focused and never give up.",
  "Don’t be afraid to give up the good to go for the great.",
  "Difficulties in life are intended to make us better, not bitter.",
  "Be so good they can’t ignore you.",
  "One day or day one. You decide.",
  "Winners never quit, and quitters never win.",
  "A little progress each day adds up to big results.",
  "Don’t tell people your dreams. Show them.",
  "When you feel like giving up, remember why you held on for so long in the first place.",
  "The only place where success comes before work is in the dictionary.",
  "Stop waiting for things to happen. Go out and make them happen.",
  "Success is not for the lazy.",
  "Do what you can with all you have, wherever you are.",
  "Sometimes later becomes never. Do it now.",
  "Success is what comes after you stop making excuses.",
  "Don’t let the fear of losing be greater than the excitement of winning.",
  "Your time is limited, don’t waste it living someone else’s life.",
  "Motivation gets you started, discipline keeps you going.",
  "Failure will never overtake me if my determination to succeed is strong enough.",
  "Be fearless in the pursuit of what sets your soul on fire.",
  "Don’t stop until you’re proud.",
  "Chase your dreams until you catch them… and then dream, catch, and dream again!",
  "Small progress is still progress.",
  "Don’t wait. The time will never be just right.",
  "Don’t call it a dream, call it a plan.",
  "Your dream doesn’t have an expiration date. Take a deep breath and try again.",
  "Push yourself because no one else is going to do it for you.",
  "Work while they sleep. Learn while they party. Save while they spend. Live like they dream.",
  "A winner is a dreamer who never gives up.",
  "Success is not measured by what you accomplish, but by the opposition you have encountered.",
  "Do something today that will inch you closer to a better tomorrow.",
  "One step at a time is all it takes to get you there.",
  "It’s never too late to be what you might have been.",
  "Keep going. Everything you need will come to you at the perfect time."
]

MOTIV_META_CHAN = "motiv_channel_id"   # meta key for channel id
MOTIV_META_HOUR = "motiv_hour_utc"     # meta key for posting hour (0–23)
MOTIV_META_IDX  = "motiv_idx"          # meta key for next quote index

_motiv_task: asyncio.Task | None = None

async def _meta_get(db, key: str, default: str|None=None) -> str|None:
    cur = await db.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = await cur.fetchone()
    return row[0] if row else default

async def _meta_set(db, key: str, value: str):
    await db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
    await db.commit()

async def _get_motiv_settings() -> tuple[int|None, int]:
    """Return (channel_id or None, hour_utc)"""
    async with aiosqlite.connect(DB_PATH) as db:
        chan_s = await _meta_get(db, MOTIV_META_CHAN)
        hour_s = await _meta_get(db, MOTIV_META_HOUR, "9")  # default 09:00 UTC
    chan_id = int(chan_s) if chan_s else None
    hour = int(hour_s) if hour_s else 9
    hour = max(0, min(23, hour))
    return chan_id, hour

async def _next_quote() -> str:
    if not QUOTES:
        return "Stay strong. One clean day at a time. 💪"
    async with aiosqlite.connect(DB_PATH) as db:
        idx_s = await _meta_get(db, MOTIV_META_IDX, "0")
        idx = int(idx_s)
        quote = QUOTES[idx % len(QUOTES)]
        await _meta_set(db, MOTIV_META_IDX, str((idx + 1) % (10**9)))
    return quote

async def _sleep_until(hour_utc: int):
    """Sleep until the next time it's <hour_utc>:00 (UTC)."""
    now = dt.datetime.now(dt.timezone.utc)
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())

async def _post_motivation_once(guild: discord.Guild) -> bool:
    chan_id, _ = await _get_motiv_settings()
    if not chan_id:
        await post_log(guild, "⚠️ Motivation: channel not set. Use /motivation_setchannel here.")
        return False
    channel = guild.get_channel(chan_id)
    if not channel:
        await post_log(guild, f"⚠️ Motivation: channel {chan_id} not found or bot lacks access.")
        return False
    try:
        quote = await _next_quote()
        await channel.send(f"🧠 **Daily Motivation**\n> {quote}")
        return True
    except Exception as e:
        await post_log(guild, f"⚠️ Motivation post failed: {e}")
        return False

async def motivation_loop():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    # initial delay to next scheduled hour
    _, hour = await _get_motiv_settings()
    while not bot.is_closed():
        await _sleep_until(hour)
        await _post_motivation_once(guild)
        # loop does 24h cadence by sleeping to next hour again
        # but also refresh hour setting in case you changed it
        _, hour = await _get_motiv_settings()

# -------- Slash commands --------
mot = app_commands.Group(name="motivation", description="Daily motivation controls")

@mot.command(name="setchannel", description="Bind the current channel for daily motivation posts")
@app_commands.checks.has_permissions(manage_guild=True)
async def motivation_setchannel(inter: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        await _meta_set(db, MOTIV_META_CHAN, str(inter.channel_id))
    await inter.response.send_message(f"✅ Motivation channel set to <#{inter.channel_id}>.", ephemeral=True)

@mot.command(name="sethour", description="Set the UTC hour (0–23) for daily posts")
@app_commands.checks.has_permissions(manage_guild=True)
async def motivation_sethour(inter: discord.Interaction, hour_utc: app_commands.Range[int, 0, 23]):
    async with aiosqlite.connect(DB_PATH) as db:
        await _meta_set(db, MOTIV_META_HOUR, str(hour_utc))
    await inter.response.send_message(f"✅ Daily motivation will post at **{hour_utc:02d}:00 UTC**.", ephemeral=True)

@mot.command(name="start", description="Start the daily motivation loop")
@app_commands.checks.has_permissions(manage_guild=True)
async def motivation_start(inter: discord.Interaction):
    global _motiv_task
    if _motiv_task and not _motiv_task.done():
        await inter.response.send_message("ℹ️ Motivation loop already running.", ephemeral=True)
        return
    _motiv_task = bot.loop.create_task(motivation_loop())
    await inter.response.send_message("✅ Motivation loop started.", ephemeral=True)

@mot.command(name="stop", description="Stop the daily motivation loop")
@app_commands.checks.has_permissions(manage_guild=True)
async def motivation_stop(inter: discord.Interaction):
    global _motiv_task
    if _motiv_task and not _motiv_task.done():
        _motiv_task.cancel()
        _motiv_task = None
        await inter.response.send_message("🛑 Motivation loop stopped.", ephemeral=True)
    else:
        await inter.response.send_message("ℹ️ Motivation loop was not running.", ephemeral=True)

@mot.command(name="now", description="Post one motivation message right now")
@app_commands.checks.has_permissions(manage_guild=True)
async def motivation_now(inter: discord.Interaction):
    ok = await _post_motivation_once(inter.guild)
    await inter.response.send_message("✅ Sent." if ok else "⚠️ Failed to send. Check logs.", ephemeral=True)

tree.add_command(mot)
# ================== /Daily Motivation ==================

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