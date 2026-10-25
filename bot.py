"""
MWRP Sales Bot
Commands: /plan  /sell  /leaderboard  /settings
Every /sell is also posted to a #sales-log channel.
All money values are in-game dollars.
"""
import os
import sqlite3
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "sales.db")
LOG_CHANNEL_NAME = os.getenv("LOG_CHANNEL_NAME", "sales-log")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")  # optional: force a specific channel

# ------------------------------------------------------------------ game numbers
# Edit these to change prices / recipes. Levels you don't list are estimated by
# a straight line between the nearest listed levels, so add exact values as you learn them.
#   RATES   = {drug: {level: (min price per bag, max price per bag)}}
#   RECIPES = {drug: (buds per joint, joints per bag)}
RATES = {
    "weed": {1: (648, 852), 10: (780, 1006)},
}
RECIPES = {
    "weed": (1, 2),
}
WASH_FEE = 0.20          # dirty money lost when washing
WASH_FEE_SKILL = 0.16    # ...with the wash skill
MAX_LEVEL = 10           # levels shown in the dropdown (Discord max 25)

# ------------------------------------------------------------------ database
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS sales (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        item       TEXT    NOT NULL,   -- e.g. "weed L10"
        qty        INTEGER NOT NULL,   -- bags sold
        unit_cost  REAL    NOT NULL,
        unit_price REAL    NOT NULL,   -- dirty money per bag
        note       TEXT,
        ts         INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS user_settings (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        bonus REAL NOT NULL DEFAULT 0, wash_skill INTEGER NOT NULL DEFAULT 0,
        level INTEGER,
        PRIMARY KEY (guild_id, user_id)
    );
    """
)
try:  # upgrade older databases
    db.execute("ALTER TABLE user_settings ADD COLUMN level INTEGER")
except sqlite3.OperationalError:
    pass
db.commit()


# ------------------------------------------------------------------ helpers
def money(x: float) -> str:
    return f"${round(x):,}"


def period_start(period: str) -> int:
    now = int(time.time())
    return {"today": now - now % 86400, "week": now - 7 * 86400,
            "month": now - 30 * 86400}.get(period, 0)


def rate_for(drug: str, level: int):
    """(min, max, estimated?) price per bag for a drug level."""
    rates = RATES[drug]
    if level in rates:
        return (*rates[level], False)
    levels = sorted(rates)
    lower = [l for l in levels if l < level]
    upper = [l for l in levels if l > level]
    if lower and upper:
        a, b = lower[-1], upper[0]
        t = (level - a) / (b - a)
        return (rates[a][0] + (rates[b][0] - rates[a][0]) * t,
                rates[a][1] + (rates[b][1] - rates[a][1]) * t, True)
    nearest = lower[-1] if lower else upper[0]
    return (*rates[nearest], True)


def fee_for(skill: bool) -> float:
    return WASH_FEE_SKILL if skill else WASH_FEE


def get_settings(guild_id: int, user_id: int):
    r = db.execute("SELECT bonus, wash_skill, level FROM user_settings WHERE guild_id=? AND user_id=?",
                   (guild_id, user_id)).fetchone()
    return (r["bonus"], bool(r["wash_skill"]), r["level"]) if r else (0.0, False, None)


def save_settings(guild_id: int, user_id: int, bonus: float, skill: bool, level):
    db.execute("INSERT INTO user_settings(guild_id,user_id,bonus,wash_skill,level) VALUES (?,?,?,?,?) "
               "ON CONFLICT(guild_id,user_id) DO UPDATE SET bonus=excluded.bonus, "
               "wash_skill=excluded.wash_skill, level=excluded.level",
               (guild_id, user_id, bonus, int(skill), level))
    db.commit()


DRUG_CHOICES = [app_commands.Choice(name=d.title(), value=d) for d in RATES]
LEVEL_CHOICES = [app_commands.Choice(name=f"Level {n}", value=n) for n in range(1, MAX_LEVEL + 1)]
BONUS_CHOICES = [app_commands.Choice(name="No bonus" if n == 0 else f"+{n}% bonus", value=n)
                 for n in (0, 5, 10, 15, 20, 25, 30, 40, 50)]
PERIODS = [app_commands.Choice(name=n, value=v) for n, v in
           [("Today", "today"), ("Last 7 days", "week"),
            ("Last 30 days", "month"), ("All time", "all")]]


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = Bot()


# ------------------------------------------------------------------ /settings
@bot.tree.command(description="Save YOUR level, sale bonus and wash skill (used by /plan and /sell)")
@app_commands.guild_only()
@app_commands.choices(level=LEVEL_CHOICES, bonus=BONUS_CHOICES)
@app_commands.describe(level="Your drug level", bonus="Bonus % on money per sale",
                       wash_skill="Do you have the wash skill? (16% fee instead of 20%)")
async def settings(i: discord.Interaction, level: app_commands.Choice[int] | None = None,
                   bonus: app_commands.Choice[int] | None = None, wash_skill: bool | None = None):
    cur_bonus, cur_skill, cur_level = get_settings(i.guild_id, i.user.id)
    new_bonus = cur_bonus if bonus is None else bonus.value
    new_skill = cur_skill if wash_skill is None else wash_skill
    new_level = cur_level if level is None else level.value
    save_settings(i.guild_id, i.user.id, new_bonus, new_skill, new_level)
    await i.response.send_message(
        f"⚙️ Saved for {i.user.mention}: level **{new_level if new_level else 'not set'}**, sale bonus "
        f"**{new_bonus:g}%**, wash skill **{'yes' if new_skill else 'no'}** "
        f"(wash fee {fee_for(new_skill) * 100:g}%)", ephemeral=True)


# ------------------------------------------------------------------ /plan
@bot.tree.command(description="Buds -> joints -> bags -> dirty & clean money calculator")
@app_commands.guild_only()
@app_commands.choices(drug=DRUG_CHOICES, level=LEVEL_CHOICES, bonus=BONUS_CHOICES)
@app_commands.describe(buds="How many buds you have (use this OR bags)",
                       bags="How many bags you'll sell (use this OR buds)",
                       drug="Drug (default: weed)",
                       level="Level (default: the one saved in /settings)",
                       bonus="Override your saved bonus for this calc",
                       wash_skill="Override your saved wash skill for this calc")
async def plan(i: discord.Interaction,
               buds: app_commands.Range[int, 0, 100_000_000] | None = None,
               bags: app_commands.Range[int, 0, 100_000_000] | None = None,
               drug: app_commands.Choice[str] | None = None,
               level: app_commands.Choice[int] | None = None,
               bonus: app_commands.Choice[int] | None = None, wash_skill: bool | None = None):
    if (buds is None) == (bags is None):
        return await i.response.send_message("Give me **either** `buds` **or** `bags` (not both).",
                                             ephemeral=True)
    s_bonus, s_skill, s_level = get_settings(i.guild_id, i.user.id)
    lvl = level.value if level is not None else s_level
    if lvl is None:
        return await i.response.send_message(
            "I don't know your level yet. Pick a `level` here, or save it once with `/settings`.",
            ephemeral=True)
    drug_name = drug.value if drug is not None else next(iter(RATES))
    b = bonus.value if bonus is not None else s_bonus
    skill = s_skill if wash_skill is None else wash_skill
    fee = fee_for(skill)
    lo, hi, est = rate_for(drug_name, lvl)
    bpj, jpb = RECIPES.get(drug_name, (1, 2))

    e = discord.Embed(title=f"🧮 {drug_name.title()} L{lvl}", color=0x9b59b6)
    if buds is not None:
        joints = buds // bpj
        n_bags = joints // jpb
        e.add_field(name="🌿 Crafting", inline=False, value=(
            f"{buds:,} buds → {joints:,} joints → **{n_bags:,} bags**\n"
            f"Leftover: {joints % jpb:,} joint(s) + {buds % bpj:,} bud(s)"))
    else:
        n_bags = bags
        e.add_field(name="🌿 Crafting", inline=False, value=(
            f"{n_bags:,} bags needs {n_bags * jpb:,} joints = **{n_bags * jpb * bpj:,} buds**"))

    mult = 1 + b / 100
    d_lo, d_hi = lo * n_bags * mult, hi * n_bags * mult
    d_avg = (d_lo + d_hi) / 2
    c_lo, c_hi, c_avg = (x * (1 - fee) for x in (d_lo, d_hi, d_avg))
    e.add_field(name="💵 Dirty money (after sale)", inline=False,
                value=f"Min {money(d_lo)} • Avg {money(d_avg)} • Max {money(d_hi)}")
    e.add_field(name="🧼 Clean money (after wash)", inline=False, value=(
        f"Min {money(c_lo)} • Avg {money(c_avg)} • Max {money(c_hi)}\n"
        f"Wash lost: {money(d_avg - c_avg)} avg"))
    e.set_footer(text=(
        f"Per bag: {money(lo * mult)}–{money(hi * mult)} (base {money(lo)}–{money(hi)}) • "
        f"bonus {b:g}% • wash fee {fee * 100:g}%" + (" • level price is an estimate" if est else "")))
    await i.response.send_message(embed=e)


# ------------------------------------------------------------------ sales log channel
async def get_log_channel(guild: discord.Guild):
    """Find (or create) the sales log channel. Returns None if it can't be used."""
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID.isdigit():
        ch = guild.get_channel(int(LOG_CHANNEL_ID))
        if ch:
            return ch
    ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if ch:
        return ch
    try:  # read-only for everyone except the bot, so the log can't be tampered with
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False),
            guild.me: discord.PermissionOverwrite(send_messages=True, embed_links=True,
                                                  view_channel=True),
        }
        return await guild.create_text_channel(LOG_CHANNEL_NAME, overwrites=overwrites,
                                               reason="Sales log for the MWRP sales bot")
    except discord.HTTPException:
        return None


# ------------------------------------------------------------------ /sell
@bot.tree.command(description="Log a sale. It's also posted in the sales-log channel")
@app_commands.guild_only()
@app_commands.choices(drug=DRUG_CHOICES, level=LEVEL_CHOICES)
@app_commands.describe(bags="Bags sold", dirty="Total dirty money you received",
                       drug="Drug (default: weed)",
                       level="Level sold (default: the one saved in /settings)",
                       wash_skill="Override your saved wash skill", note="Buyer / location / anything")
async def sell(i: discord.Interaction, bags: app_commands.Range[int, 1, 1_000_000],
               dirty: app_commands.Range[float, 0, 1_000_000_000],
               drug: app_commands.Choice[str] | None = None,
               level: app_commands.Choice[int] | None = None,
               wash_skill: bool | None = None, note: str | None = None):
    s_bonus, s_skill, s_level = get_settings(i.guild_id, i.user.id)
    lvl = level.value if level is not None else s_level
    if lvl is None:
        return await i.response.send_message(
            "I don't know your level yet. Pick a `level` here, or save it once with `/settings`.",
            ephemeral=True)
    await i.response.defer(ephemeral=True)
    drug_name = drug.value if drug is not None else next(iter(RATES))
    skill = s_skill if wash_skill is None else wash_skill
    per_bag = dirty / bags
    clean = dirty * (1 - fee_for(skill))

    db.execute("INSERT INTO sales(guild_id,user_id,item,qty,unit_cost,unit_price,note,ts) "
               "VALUES (?,?,?,?,?,?,?,?)",
               (i.guild_id, i.user.id, f"{drug_name} L{lvl}", bags, 0, per_bag, note, int(time.time())))
    db.commit()

    e = discord.Embed(title="💰 Sale logged", color=0x2ecc71,
                      description=f"{i.user.mention} sold **{bags:,}× {drug_name.title()} L{lvl}**")
    e.add_field(name="Dirty money", value=money(dirty))
    e.add_field(name="Per bag", value=money(per_bag))
    e.add_field(name="Clean (after wash)", value=money(clean))
    if note:
        e.add_field(name="Note", value=note, inline=False)
    e.timestamp = datetime.now(timezone.utc)

    posted = None
    channel = await get_log_channel(i.guild)
    if channel:
        try:
            await channel.send(embed=e)
            posted = channel
        except discord.HTTPException:
            posted = None

    # sanity check against the known price range
    lo, hi, _ = rate_for(drug_name, lvl)
    top = hi * (1 + s_bonus / 100)
    warn = ""
    if per_bag > top * 1.01:
        warn = f"\n⚠️ {money(per_bag)}/bag is above the max I'd expect ({money(top)}). Double-check the amount."
    elif per_bag < lo * 0.99:
        warn = f"\n⚠️ {money(per_bag)}/bag is below the usual minimum ({money(lo)}). Double-check the amount."

    if posted:
        msg = f"✅ Logged and posted in {posted.mention}.{warn}"
    else:
        msg = (f"✅ Logged, but I couldn't post in a `#{LOG_CHANNEL_NAME}` channel. Create one and give me "
               f"permission to send messages there.{warn}")
    await i.followup.send(msg, ephemeral=True)


# ------------------------------------------------------------------ /leaderboard
@bot.tree.command(description="Top sellers by dirty money")
@app_commands.guild_only()
@app_commands.choices(period=PERIODS)
async def leaderboard(i: discord.Interaction, period: app_commands.Choice[str] | None = None):
    p = period.value if period else "week"
    rows = db.execute(
        "SELECT user_id, SUM(qty*unit_price) dirty, SUM(qty) bags, COUNT(*) n FROM sales "
        "WHERE guild_id=? AND ts>=? GROUP BY user_id ORDER BY dirty DESC LIMIT 10",
        (i.guild_id, period_start(p))).fetchall()
    if not rows:
        return await i.response.send_message("No sales in that period.", ephemeral=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[n] if n < 3 else f'`{n + 1}.`'} <@{r['user_id']}> — **{money(r['dirty'])}** "
             f"({r['bags']:,} bags, {r['n']} sales)" for n, r in enumerate(rows)]
    total = sum(r["dirty"] for r in rows)
    e = discord.Embed(title=f"🏆 Leaderboard — {p}", description="\n".join(lines), color=0xe67e22)
    e.set_footer(text=f"Crew total (top 10): {money(total)} dirty")
    await i.response.send_message(embed=e)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file or your host's environment variables.")
    bot.run(TOKEN)
