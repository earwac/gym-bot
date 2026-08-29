"""
Gym Challenge Discord Bot
-------------------------
Watches a channel. When a user posts an image, it logs one gym day for that
user for today (weeks run Monday–Sunday). Multiple photos on the same day
count once. Goal is 3 gym days/week; each day short of 3 costs $5.
The challenge starts on CHALLENGE_START — photos before then don't count.

Commands (slash):
  /status [member]  -> this week's days + owed for you (or a member)
  /week [weeks_ago] -> image: each person's Mon-Sun grid + what they owe
  /total            -> image: all weeks stacked, day-by-day grid for everyone
  /overall          -> image: season totals (days, weeks met goal, total owed)
  /leaderboard      -> most gym days this week
  /setchannel       -> (admin) make THIS channel the tracked one
  /backfill         -> (admin) scan this channel's past photos and log missed days
  /undo             -> remove today's logged day for yourself (mistake fixer)
  /undo_for         -> (admin) remove a logged day for someone else
  /add_day          -> (admin) manually log a gym day for someone

Data lives in gym.db (SQLite) next to this file.
"""

import io
import os
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

# ---------- config ----------
GOAL_PER_WEEK = 3
PENALTY_PER_MISSED_DAY = 25  # dollars
CHALLENGE_START = date(2026, 8, 24)  # Monday the challenge begins; earlier days don't count
CHALLENGE_END = date(2026, 12, 31)   # Last day photos count; set to None for no end date
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gym.db")
TOKEN = os.environ.get("DISCORD_TOKEN")

# Every "what day is it" calculation goes through this, so the bot tracks
# days by US Eastern time regardless of what timezone the server itself is
# in (handles EST/EDT switches automatically).
TIMEZONE = ZoneInfo("America/New_York")

def local_today() -> date:
    return datetime.now(TIMEZONE).date()

def effective_today() -> date:
    """Today (in TIMEZONE), capped at CHALLENGE_END so week/owed math stops
    counting once the challenge is over (instead of racking up unlimited
    future weeks)."""
    t = local_today()
    if CHALLENGE_END is not None and t > CHALLENGE_END:
        return CHALLENGE_END
    return t

# ---------- database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS logs (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                username TEXT NOT NULL,
                day      TEXT NOT NULL,          -- ISO date 'YYYY-MM-DD'
                week_start TEXT NOT NULL,        -- ISO date of Monday
                PRIMARY KEY (guild_id, user_id, day)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            )"""
        )

def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())

def get_tracked_channel(guild_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT channel_id FROM settings WHERE guild_id=?", (guild_id,)
        ).fetchone()
        return row["channel_id"] if row else None

def set_tracked_channel(guild_id: int, channel_id: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(guild_id, channel_id) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
            (guild_id, channel_id),
        )

def log_day(guild_id, user_id, username, d: date) -> bool:
    """Returns True if a new day was logged, False if already logged."""
    wk = monday_of(d).isoformat()
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO logs(guild_id,user_id,username,day,week_start) VALUES(?,?,?,?,?)",
                (guild_id, user_id, username, d.isoformat(), wk),
            )
            return True
        except sqlite3.IntegrityError:
            return False

def remove_day(guild_id, user_id, d: date) -> bool:
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM logs WHERE guild_id=? AND user_id=? AND day=?",
            (guild_id, user_id, d.isoformat()),
        )
        return cur.rowcount > 0

def days_this_week(guild_id, user_id, wk_start: str) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM logs WHERE guild_id=? AND user_id=? AND week_start=?",
            (guild_id, user_id, wk_start),
        ).fetchone()
        return row["c"]

def week_counts(guild_id, wk_start: str):
    with db() as conn:
        return conn.execute(
            "SELECT user_id, username, COUNT(*) c FROM logs "
            "WHERE guild_id=? AND week_start=? GROUP BY user_id ORDER BY c DESC",
            (guild_id, wk_start),
        ).fetchall()

def week_detail(guild_id, wk_start: str):
    """Returns [{'name':..., 'weekdays': set(0..6)}] for the given week, one entry per user."""
    cs = CHALLENGE_START.isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT username, day FROM logs WHERE guild_id=? AND week_start=? AND day>=? "
            "ORDER BY username",
            (guild_id, wk_start, cs),
        ).fetchall()
    people = {}
    for r in rows:
        wd = date.fromisoformat(r["day"]).weekday()  # 0=Mon..6=Sun
        people.setdefault(r["username"], set()).add(wd)
    return [{"name": n, "weekdays": days} for n, days in
            sorted(people.items(), key=lambda kv: (-len(kv[1]), kv[0].lower()))]

def all_weeks_detail(guild_id, roster_override=None):
    """
    Returns (weeks, roster) where:
      weeks = [{'week_start': date, 'people': {name: set(weekdays)}}]
              one entry per elapsed challenge week, oldest first (even empty weeks)
      roster = sorted list of participant names. If roster_override is given
               (e.g. everyone in the channel), that list is used and merged
               with anyone who logged, so no-shows still appear.
    """
    cs = CHALLENGE_START.isoformat()
    today = effective_today()
    with db() as conn:
        rows = conn.execute(
            "SELECT username, day, week_start FROM logs "
            "WHERE guild_id=? AND day>=? ORDER BY day",
            (guild_id, cs),
        ).fetchall()

    by_week = {}
    roster = set(roster_override) if roster_override else set()
    for r in rows:
        roster.add(r["username"])
        wd = date.fromisoformat(r["day"]).weekday()
        by_week.setdefault(r["week_start"], {}).setdefault(r["username"], set()).add(wd)

    if today < CHALLENGE_START:
        return [], sorted(roster, key=str.lower)

    first_monday = monday_of(CHALLENGE_START)
    cur_monday = monday_of(today)
    weeks_elapsed = (cur_monday - first_monday).days // 7 + 1

    weeks = []
    for i in range(weeks_elapsed):
        wk = first_monday + timedelta(weeks=i)
        weeks.append({
            "week_start": wk,
            "people": by_week.get(wk.isoformat(), {}),
        })
    return weeks, sorted(roster, key=str.lower)

def season_totals(guild_id, roster_override=None):
    """
    Per user since CHALLENGE_START:
      name, total_days, weeks_met (weeks with >=GOAL), weeks_active, owed
    Owed sums (3 - days)*$5 over every week from the first challenge week
    through the current week, so weeks with zero posts still count as misses.
    If roster_override is given, anyone on it who never logged is added as a
    full no-show (0 days, owing the max for every elapsed week).
    """
    cs = CHALLENGE_START.isoformat()
    today = effective_today()
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, week_start, COUNT(*) c FROM logs "
            "WHERE guild_id=? AND day>=? GROUP BY user_id, week_start",
            (guild_id, cs),
        ).fetchall()
    # how many challenge weeks have elapsed (at least 1 once we're past start)
    first_monday = monday_of(CHALLENGE_START)
    cur_monday = monday_of(today)
    weeks_elapsed = max(0, (cur_monday - first_monday).days // 7 + 1) if today >= CHALLENGE_START else 0

    per = {}  # user_id -> {name, days, weeks_met, weeks_active, week_counts:{wk:c}}
    for r in rows:
        u = r["user_id"]
        e = per.setdefault(u, {"name": r["username"], "days": 0, "weeks_met": 0,
                               "weeks_active": 0, "weeks": {}})
        e["name"] = r["username"]
        e["days"] += r["c"]
        e["weeks_active"] += 1
        if r["c"] >= GOAL_PER_WEEK:
            e["weeks_met"] += 1
        e["weeks"][r["week_start"]] = r["c"]

    out = []
    logged_names = set()
    for u, e in per.items():
        logged_names.add(e["name"])
        # owed across ALL elapsed weeks: weeks with no logs count as 0 days
        owed = 0
        for i in range(weeks_elapsed):
            wk = (first_monday + timedelta(weeks=i)).isoformat()
            c = e["weeks"].get(wk, 0)
            owed += max(0, GOAL_PER_WEEK - c) * PENALTY_PER_MISSED_DAY
        out.append({
            "name": e["name"], "days": e["days"], "weeks_met": e["weeks_met"],
            "weeks_elapsed": weeks_elapsed, "owed": owed,
        })

    # add channel members who never logged anything
    if roster_override:
        full_owed = weeks_elapsed * GOAL_PER_WEEK * PENALTY_PER_MISSED_DAY
        for name in roster_override:
            if name not in logged_names:
                out.append({
                    "name": name, "days": 0, "weeks_met": 0,
                    "weeks_elapsed": weeks_elapsed, "owed": full_owed,
                })

    out.sort(key=lambda x: (-x["owed"], -x["days"], x["name"].lower()))
    return out, weeks_elapsed

# ---------- report image ----------
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def _font(size, bold=False):
    candidates = (
        ["arialbd.ttf", "Arial_Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
        else ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()

def render_week_report(week_start: date, rows):
    """rows: [{'name': str, 'weekdays': set[int]}] with 0=Mon..6=Sun. Returns PIL.Image."""
    pad, row_h, header_h = 30, 54, 96
    name_w, cell_w, cell_gap, owed_w = 190, 92, 8, 150
    n = max(1, len(rows))
    width = pad*2 + name_w + (cell_w+cell_gap)*7 + owed_w
    height = header_h + row_h*n + pad*2 + 30

    BG, CARD, TXT, MUTED = (24,26,32), (34,37,46), (235,237,242), (150,155,165)
    GREEN, GREEN_BG, RED = (46,160,90), (30,60,44), (210,90,80)

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    f_title, f_sub, f_head = _font(30, True), _font(18), _font(19, True)
    f_name, f_cell, f_owed = _font(21, True), _font(22, True), _font(20, True)

    week_end = week_start + timedelta(days=6)
    d.text((pad, 24), "Weekly Gym Report", font=f_title, fill=TXT)
    sub = (f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"
           f"   ·   Goal {GOAL_PER_WEEK}/week   ·   ${PENALTY_PER_MISSED_DAY}/missed day")
    d.text((pad, 62), sub, font=f_sub, fill=MUTED)

    x0 = pad + name_w
    for i, lbl in enumerate(DAY_LABELS):
        cx = x0 + i*(cell_w+cell_gap) + cell_w//2
        w = d.textlength(lbl, font=f_head)
        d.text((cx - w/2, header_h-4), lbl, font=f_head, fill=MUTED)
    owed_x = x0 + 7*(cell_w+cell_gap)
    w = d.textlength("Owed", font=f_head)
    d.text((owed_x + owed_w/2 - w/2, header_h-4), "Owed", font=f_head, fill=MUTED)

    if not rows:
        d.text((pad, header_h+40), "No gym days logged this week yet.",
               font=f_name, fill=MUTED)
        return img

    y = header_h + 28
    for r in rows:
        d.text((pad, y + row_h/2 - 13), r["name"][:16], font=f_name, fill=TXT)
        count = len(r["weekdays"])
        for i in range(7):
            cx, cy = x0 + i*(cell_w+cell_gap), y + 6
            went = i in r["weekdays"]
            d.rounded_rectangle([cx, cy, cx+cell_w, cy+row_h-12], radius=10,
                                fill=(GREEN_BG if went else CARD))
            mark, col = ("\u2713", GREEN) if went else ("\u2013", MUTED)
            mw = d.textlength(mark, font=f_cell)
            d.text((cx + cell_w/2 - mw/2, cy + (row_h-12)/2 - 15), mark, font=f_cell, fill=col)
        owed = max(0, GOAL_PER_WEEK - count) * PENALTY_PER_MISSED_DAY
        txt, col = ("On track", GREEN) if owed == 0 else (f"${owed}", RED)
        tw = d.textlength(txt, font=f_owed)
        d.text((owed_x + owed_w/2 - tw/2, y + row_h/2 - 13), txt, font=f_owed, fill=col)
        y += row_h
    return img

def render_season_report(rows, weeks_elapsed):
    """rows: [{'name','days','weeks_met','weeks_elapsed','owed'}]. Returns PIL.Image."""
    pad, row_h, header_h = 30, 54, 108
    name_w, c1, c2, c3 = 230, 150, 170, 150   # name, total days, weeks met, owed
    n = max(1, len(rows))
    width = pad*2 + name_w + c1 + c2 + c3
    height = header_h + row_h*n + pad*2 + 40

    BG, CARD, TXT, MUTED = (24,26,32), (34,37,46), (235,237,242), (150,155,165)
    GREEN, RED, ACCENT = (46,160,90), (210,90,80), (120,170,240)

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    f_title, f_sub, f_head = _font(30, True), _font(18), _font(18, True)
    f_name, f_val, f_owed = _font(21, True), _font(21), _font(21, True)

    d.text((pad, 24), "Season Totals", font=f_title, fill=TXT)
    wk_word = "week" if weeks_elapsed == 1 else "weeks"
    sub = (f"Since {CHALLENGE_START.strftime('%b %d, %Y')}   ·   {weeks_elapsed} {wk_word} in"
           f"   ·   Goal {GOAL_PER_WEEK}/week   ·   ${PENALTY_PER_MISSED_DAY}/missed day")
    d.text((pad, 62), sub, font=f_sub, fill=MUTED)

    cols = [("Total days", name_w, c1), ("Weeks met goal", name_w+c1, c2),
            ("Owed", name_w+c1+c2, c3)]
    for lbl, x, w in cols:
        tw = d.textlength(lbl, font=f_head)
        d.text((pad + x + w/2 - tw/2, header_h-6), lbl, font=f_head, fill=MUTED)

    if not rows:
        d.text((pad, header_h+40), "No activity yet.", font=f_name, fill=MUTED)
        return img

    y = header_h + 34
    for r in rows:
        d.text((pad, y + row_h/2 - 13), r["name"][:18], font=f_name, fill=TXT)
        vals = [
            (str(r["days"]), name_w, c1, TXT),
            (f'{r["weeks_met"]}/{r["weeks_elapsed"]}', name_w+c1, c2, ACCENT),
        ]
        for txt, x, w, col in vals:
            tw = d.textlength(txt, font=f_val)
            d.text((pad + x + w/2 - tw/2, y + row_h/2 - 13), txt, font=f_val, fill=col)
        otxt, ocol = ("On track", GREEN) if r["owed"] == 0 else (f'${r["owed"]}', RED)
        tw = d.textlength(otxt, font=f_owed)
        d.text((pad + name_w+c1+c2 + c3/2 - tw/2, y + row_h/2 - 13), otxt, font=f_owed, fill=ocol)
        y += row_h

    # pool total footer
    pool = sum(r["owed"] for r in rows)
    d.line([(pad, y+6), (width-pad, y+6)], fill=(55,58,68), width=1)
    foot = f"Total pool owed: ${pool}"
    fw = d.textlength(foot, font=f_owed)
    d.text((width - pad - fw, y+18), foot, font=f_owed, fill=(TXT if pool == 0 else RED))
    return img

def render_season_grid(weeks, roster):
    """
    Stacked all-weeks grid. weeks: [{'week_start': date, 'people': {name:set(wd)}}].
    roster: list of names shown in every week (drop-offs stay visible).
    Returns PIL.Image.
    """
    # palette (matches the weekly/season reports)
    BG, PANEL, CARD = (24,26,32), (30,33,41), (34,37,46)
    TXT, MUTED, FAINT = (235,237,242), (150,155,165), (95,100,110)
    GREEN, GREEN_BG = (46,160,90), (30,60,44)
    RED, RED_BG = (210,90,80), (58,32,34)
    ACCENT = (120,170,240)

    pad = 34
    name_w = 150
    cell_w, cell_gap = 62, 6
    cnt_w = 88
    row_h = 44
    week_title_h = 46
    week_gap = 26
    grid_head_h = 30

    n_people = max(1, len(roster))
    content_w = name_w + (cell_w+cell_gap)*7 + cnt_w
    width = pad*2 + content_w

    # precompute height
    header_block = 108
    per_week_h = week_title_h + grid_head_h + row_h*n_people + week_gap
    height = header_block + per_week_h*max(1, len(weeks)) + pad

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    f_title, f_sub = _font(32, True), _font(18)
    f_week = _font(22, True)
    f_head = _font(16, True)
    f_name = _font(19, True)
    f_cell = _font(20, True)
    f_cnt = _font(19, True)

    # title
    d.text((pad, 26), "Season Grid — All Weeks", font=f_title, fill=TXT)
    if weeks:
        span_end = weeks[-1]["week_start"] + timedelta(days=6)
        sub = (f"{CHALLENGE_START.strftime('%b %d')} – {span_end.strftime('%b %d, %Y')}"
               f"   ·   Goal {GOAL_PER_WEEK}/week   ·   ${PENALTY_PER_MISSED_DAY}/missed day")
    else:
        sub = f"Challenge starts {CHALLENGE_START.strftime('%b %d, %Y')}"
    d.text((pad, 66), sub, font=f_sub, fill=MUTED)

    if not weeks:
        d.text((pad, header_block), "No weeks to show yet.", font=f_week, fill=MUTED)
        return img

    x0 = pad + name_w
    y = header_block

    for wk in weeks:
        ws = wk["week_start"]
        we = ws + timedelta(days=6)
        # week header bar
        d.rounded_rectangle([pad, y, width-pad, y+week_title_h-8], radius=10, fill=PANEL)
        d.text((pad+14, y+8), f"Week of {ws.strftime('%b %d')}", font=f_week, fill=TXT)
        rng = f"{ws.strftime('%b %d')} – {we.strftime('%b %d')}"
        rw = d.textlength(rng, font=f_head)
        d.text((width-pad-14-rw, y+14), rng, font=f_head, fill=MUTED)
        y += week_title_h

        # column headers
        for i, lbl in enumerate(DAY_LABELS):
            cx = x0 + i*(cell_w+cell_gap) + cell_w//2
            w = d.textlength(lbl, font=f_head)
            d.text((cx - w/2, y+4), lbl, font=f_head, fill=FAINT)
        clbl = "Days"
        w = d.textlength(clbl, font=f_head)
        d.text((x0 + 7*(cell_w+cell_gap) + cnt_w/2 - w/2, y+4), clbl, font=f_head, fill=FAINT)
        y += grid_head_h

        # one row per roster member
        for name in roster:
            wd = wk["people"].get(name, set())
            count = len(wd)
            hit = count >= GOAL_PER_WEEK
            d.text((pad, y + row_h/2 - 12), name[:14], font=f_name, fill=TXT)
            for i in range(7):
                cx, cy = x0 + i*(cell_w+cell_gap), y + 4
                went = i in wd
                d.rounded_rectangle([cx, cy, cx+cell_w, cy+row_h-10], radius=8,
                                    fill=(GREEN_BG if went else CARD))
                mark, col = ("\u2713", GREEN) if went else ("\u2013", FAINT)
                mw = d.textlength(mark, font=f_cell)
                d.text((cx + cell_w/2 - mw/2, cy + (row_h-10)/2 - 14), mark, font=f_cell, fill=col)
            # count pill (green if hit, red if not)
            px = x0 + 7*(cell_w+cell_gap)
            d.rounded_rectangle([px, y+4, px+cnt_w, y+row_h-6], radius=8,
                                fill=(GREEN_BG if hit else RED_BG))
            ctxt = f"{count}/{GOAL_PER_WEEK}"
            cw = d.textlength(ctxt, font=f_cnt)
            d.text((px + cnt_w/2 - cw/2, y + row_h/2 - 11), ctxt, font=f_cnt,
                   fill=(GREEN if hit else RED))
            y += row_h

        y += week_gap

    return img

# ---------- roster ----------
async def channel_roster(interaction: discord.Interaction):
    """
    Display names of every non-bot human who can view the tracked channel
    (falls back to the channel the command was run in). Returns a sorted list.
    """
    guild = interaction.guild
    tracked_id = get_tracked_channel(guild.id) or interaction.channel_id
    channel = guild.get_channel(tracked_id) or interaction.channel
    names = []
    seen = set()
    try:
        members = channel.members  # requires Server Members Intent
    except Exception:
        members = []
    for m in members:
        if m.bot or m.id in seen:
            continue
        seen.add(m.id)
        names.append(m.display_name)
    return sorted(names, key=str.lower)

def merge_week_roster(detail_rows, roster):
    """Ensure every roster name appears in a week_detail result (missing -> empty)."""
    have = {r["name"] for r in detail_rows}
    out = list(detail_rows)
    for name in roster:
        if name not in have:
            out.append({"name": name, "weekdays": set()})
    out.sort(key=lambda r: (-len(r["weekdays"]), r["name"].lower()))
    return out

# ---------- bot ----------
intents = discord.Intents.default()
intents.message_content = True  # needed to see attachments reliably
intents.members = True  # needed to list everyone in the channel for the roster
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user}. Synced {len(synced)} commands.")
    except Exception as e:
        print("Sync error:", e)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    tracked = get_tracked_channel(message.guild.id)
    if tracked and message.channel.id == tracked:
        has_image = any(
            (a.content_type or "").startswith("image/")
            or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"))
            for a in message.attachments
        )
        if has_image:
            today = local_today()
            if today < CHALLENGE_START:
                await message.add_reaction("⏳")
                await message.channel.send(
                    f"The challenge starts **{CHALLENGE_START.strftime('%b %d, %Y')}** — "
                    f"photos before then don't count yet. 💪",
                    delete_after=15,
                )
                await bot.process_commands(message)
                return
            if CHALLENGE_END is not None and today > CHALLENGE_END:
                await message.add_reaction("🏁")
                await message.channel.send(
                    f"The challenge ended **{CHALLENGE_END.strftime('%b %d, %Y')}** — "
                    f"nice work, but this one's in the books. 🏁",
                    delete_after=15,
                )
                await bot.process_commands(message)
                return
            newly = log_day(
                message.guild.id, message.author.id,
                message.author.display_name, today,
            )
            wk = monday_of(today).isoformat()
            count = days_this_week(message.guild.id, message.author.id, wk)
            if newly:
                emoji = "✅" if count >= GOAL_PER_WEEK else "💪"
                note = "goal hit!" if count >= GOAL_PER_WEEK else f"{GOAL_PER_WEEK - count} to go"
                await message.add_reaction(emoji)
                await message.channel.send(
                    f"Logged a gym day for **{message.author.display_name}** — "
                    f"{count}/{GOAL_PER_WEEK} this week ({note})",
                    delete_after=15,
                )
            else:
                await message.add_reaction("🔁")  # already counted today
    await bot.process_commands(message)

def owed_for(count: int) -> int:
    return max(0, GOAL_PER_WEEK - count) * PENALTY_PER_MISSED_DAY

@bot.tree.command(description="Your gym days and amount owed this week")
@app_commands.describe(member="Check someone else (optional)")
async def status(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    wk = monday_of(effective_today()).isoformat()
    count = days_this_week(interaction.guild_id, member.id, wk)
    owed = owed_for(count)
    msg = (f"**{member.display_name}** — {count}/{GOAL_PER_WEEK} gym days this week. "
           + ("On track ✅" if owed == 0 else f"Owes **${owed}** so far this week."))
    await interaction.response.send_message(msg)

@bot.tree.command(name="week", description="This week's report: each person's days + what they owe")
@app_commands.describe(weeks_ago="0 = this week (default), 1 = last week, etc.")
async def week(interaction: discord.Interaction, weeks_ago: int = 0):
    await interaction.response.defer()
    weeks_ago = max(0, weeks_ago)
    wk_monday = monday_of(effective_today()) - timedelta(weeks=weeks_ago)
    rows = week_detail(interaction.guild_id, wk_monday.isoformat())
    roster = await channel_roster(interaction)
    rows = merge_week_roster(rows, roster)
    img = render_week_report(wk_monday, rows)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    file = discord.File(buf, filename="gym_report.png")
    await interaction.followup.send(file=file)

@bot.tree.command(name="total", description="All weeks stacked: every week's day-by-day grid for everyone")
async def total(interaction: discord.Interaction):
    await interaction.response.defer()  # rendering a tall image may take a moment
    roster = await channel_roster(interaction)
    weeks, roster = all_weeks_detail(interaction.guild_id, roster_override=roster)
    img = render_season_grid(weeks, roster)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    file = discord.File(buf, filename="season_grid.png")
    await interaction.followup.send(file=file)

@bot.tree.command(description="Season totals: everyone's days, weeks met, and total owed")
async def overall(interaction: discord.Interaction):
    await interaction.response.defer()
    roster = await channel_roster(interaction)
    rows, weeks_elapsed = season_totals(interaction.guild_id, roster_override=roster)
    img = render_season_report(rows, weeks_elapsed)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    file = discord.File(buf, filename="season_report.png")
    await interaction.followup.send(file=file)

@bot.tree.command(description="Most gym days this week")
async def leaderboard(interaction: discord.Interaction):
    wk = monday_of(effective_today()).isoformat()
    rows = week_counts(interaction.guild_id, wk)
    if not rows:
        await interaction.response.send_message("No gym days logged yet this week.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        badge = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{badge} **{r['username']}** — {r['c']} days")
    await interaction.response.send_message("**This week's leaderboard**\n" + "\n".join(lines))

@bot.tree.command(description="Undo today's logged gym day for yourself")
async def undo(interaction: discord.Interaction):
    removed = remove_day(interaction.guild_id, interaction.user.id, local_today())
    if removed:
        await interaction.response.send_message("Removed today's gym day for you.", ephemeral=True)
    else:
        await interaction.response.send_message("You had no gym day logged today.", ephemeral=True)

@bot.tree.command(description="(Admin) Remove a logged gym day for someone else")
@app_commands.describe(
    member="Whose logged day to remove",
    days_ago="0 = today (default), 1 = yesterday, etc.",
)
@app_commands.checks.has_permissions(administrator=True)
async def undo_for(interaction: discord.Interaction, member: discord.Member, days_ago: int = 0):
    target_day = local_today() - timedelta(days=max(0, days_ago))
    removed = remove_day(interaction.guild_id, member.id, target_day)
    if removed:
        await interaction.response.send_message(
            f"Removed **{member.display_name}**'s gym day for {target_day.strftime('%b %d, %Y')}.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"**{member.display_name}** had no gym day logged for {target_day.strftime('%b %d, %Y')}.",
            ephemeral=True,
        )

@undo_for.error
async def undo_for_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need admin permission to undo someone else's day.", ephemeral=True
        )

@bot.tree.command(description="(Admin) Manually log a gym day for someone")
@app_commands.describe(
    member="Who to log a gym day for",
    days_ago="0 = today (default), 1 = yesterday, etc.",
)
@app_commands.checks.has_permissions(administrator=True)
async def add_day(interaction: discord.Interaction, member: discord.Member, days_ago: int = 0):
    target_day = local_today() - timedelta(days=max(0, days_ago))
    if target_day < CHALLENGE_START:
        await interaction.response.send_message(
            f"That's before the challenge started ({CHALLENGE_START.strftime('%b %d, %Y')}) — "
            f"not logged.",
            ephemeral=True,
        )
        return
    if CHALLENGE_END is not None and target_day > CHALLENGE_END:
        await interaction.response.send_message(
            f"That's after the challenge ended ({CHALLENGE_END.strftime('%b %d, %Y')}) — "
            f"not logged.",
            ephemeral=True,
        )
        return
    added = log_day(interaction.guild_id, member.id, member.display_name, target_day)
    if added:
        await interaction.response.send_message(
            f"Logged a gym day for **{member.display_name}** on {target_day.strftime('%b %d, %Y')}.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"**{member.display_name}** already had a gym day logged for "
            f"{target_day.strftime('%b %d, %Y')}.",
            ephemeral=True,
        )

@add_day.error
async def add_day_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need admin permission to add a day for someone else.", ephemeral=True
        )

@bot.tree.command(description="(Admin) Track THIS channel for gym photos")
@app_commands.checks.has_permissions(administrator=True)
async def setchannel(interaction: discord.Interaction):
    set_tracked_channel(interaction.guild_id, interaction.channel_id)
    await interaction.response.send_message(
        f"✅ Now tracking <#{interaction.channel_id}>. "
        f"Post a gym photo here and it logs a day. Goal: {GOAL_PER_WEEK}/week, "
        f"${PENALTY_PER_MISSED_DAY} per missed day."
    )

@setchannel.error
async def setchannel_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need admin permission to set the channel.", ephemeral=True
        )

@bot.tree.command(description="(Admin) Scan this channel's past photos and log any missed days")
@app_commands.checks.has_permissions(administrator=True)
async def backfill(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tracked = get_tracked_channel(interaction.guild_id)
    if tracked != interaction.channel_id:
        await interaction.followup.send(
            "Run this in the tracked channel (the one you set with /setchannel).",
            ephemeral=True,
        )
        return

    start_dt = datetime.combine(CHALLENGE_START, datetime.min.time())
    end_dt = None
    if CHALLENGE_END is not None:
        end_dt = datetime.combine(CHALLENGE_END + timedelta(days=1), datetime.min.time())

    scanned = 0
    newly_logged = 0
    already_had = 0
    async for message in interaction.channel.history(
        after=start_dt, before=end_dt, limit=None, oldest_first=True
    ):
        if message.author.bot:
            continue
        has_image = any(
            (a.content_type or "").startswith("image/")
            or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"))
            for a in message.attachments
        )
        if not has_image:
            continue
        scanned += 1
        d = message.created_at.astimezone(TIMEZONE).date()
        if log_day(interaction.guild_id, message.author.id, message.author.display_name, d):
            newly_logged += 1
        else:
            already_had += 1

    await interaction.followup.send(
        f"Backfill done. Scanned **{scanned}** past photo message(s), "
        f"logged **{newly_logged}** new gym day(s) "
        f"({already_had} were already counted). Run `/week` or `/overall` to see it reflected.",
        ephemeral=True,
    )

@backfill.error
async def backfill_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need admin permission to run a backfill.", ephemeral=True
        )

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(TOKEN)
