"""
MWRP drug economy module.

- Sell price per bag depends on the drug's level (a min-max range).
- Optional per-sale money bonus (e.g. +20%).
- Dirty money loses a fee when washed: 20%, or 16% with the wash skill.
- Crafting chain: buds -> joints -> bags (only bags can be sold).
  Defaults for weed: 1 bud = 1 joint, 2 joints = 1 bag.

Level prices you set exactly are used as-is. Levels in between are
estimated by straight-line interpolation between the nearest levels you set
(and clearly marked as estimates). Set exact values with /drug rate.
"""
import sqlite3
import time

import discord
from discord import app_commands

DEFAULT_RATES = {"weed": {1: (648, 852), 10: (780, 1006)}}
DEFAULT_RECIPE = {"weed": (1, 2)}          # (buds per joint, joints per bag)
DEFAULT_RECIPE_FALLBACK = (1, 2)
WASH_FEE = 0.20
WASH_FEE_SKILL = 0.16

# Dropdown options (Discord allows at most 25 per dropdown). Raise MAX_LEVEL if your server goes higher.
MAX_LEVEL = 10
LEVEL_CHOICES = [app_commands.Choice(name=f"Level {n}", value=n) for n in range(1, MAX_LEVEL + 1)]
BONUS_CHOICES = [app_commands.Choice(name="No bonus" if n == 0 else f"+{n}% bonus", value=n)
                 for n in (0, 5, 10, 15, 20, 25, 30, 40, 50)]


def setup(bot, db, money):
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS drug_rates (
            guild_id INTEGER NOT NULL, drug TEXT NOT NULL, level INTEGER NOT NULL,
            min_price REAL NOT NULL, max_price REAL NOT NULL,
            PRIMARY KEY (guild_id, drug, level)
        );
        CREATE TABLE IF NOT EXISTS drug_recipe (
            guild_id INTEGER NOT NULL, drug TEXT NOT NULL,
            buds_per_joint INTEGER NOT NULL, joints_per_bag INTEGER NOT NULL,
            PRIMARY KEY (guild_id, drug)
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            bonus REAL NOT NULL DEFAULT 0, wash_skill INTEGER NOT NULL DEFAULT 0,
            level INTEGER,
            PRIMARY KEY (guild_id, user_id)
        );
        """
    )
    try:  # upgrade databases created before the level setting existed
        db.execute("ALTER TABLE user_settings ADD COLUMN level INTEGER")
    except sqlite3.OperationalError:
        pass
    db.commit()

    # ------------------------------------------------------------ helpers
    def norm(s: str) -> str:
        return s.lower().strip()

    def get_rates(guild_id: int, drug: str) -> dict:
        rows = db.execute("SELECT level, min_price, max_price FROM drug_rates WHERE guild_id=? AND drug=?",
                          (guild_id, drug)).fetchall()
        if rows:
            return {r["level"]: (r["min_price"], r["max_price"]) for r in rows}
        return dict(DEFAULT_RATES.get(drug, {}))

    def rate_for(rates: dict, level: int):
        """Return (min, max, estimated) or None if the drug has no rates."""
        if not rates:
            return None
        if level in rates:
            return (*rates[level], False)
        levels = sorted(rates)
        lower = [l for l in levels if l < level]
        upper = [l for l in levels if l > level]
        if lower and upper:
            a, b = lower[-1], upper[0]
            t = (level - a) / (b - a)
            lo = rates[a][0] + (rates[b][0] - rates[a][0]) * t
            hi = rates[a][1] + (rates[b][1] - rates[a][1]) * t
            return (lo, hi, True)
        nearest = lower[-1] if lower else upper[0]
        return (*rates[nearest], True)

    def get_recipe(guild_id: int, drug: str):
        r = db.execute("SELECT buds_per_joint, joints_per_bag FROM drug_recipe WHERE guild_id=? AND drug=?",
                       (guild_id, drug)).fetchone()
        if r:
            return r["buds_per_joint"], r["joints_per_bag"]
        return DEFAULT_RECIPE.get(drug, DEFAULT_RECIPE_FALLBACK)

    def get_settings(guild_id: int, user_id: int):
        r = db.execute("SELECT bonus, wash_skill FROM user_settings WHERE guild_id=? AND user_id=?",
                       (guild_id, user_id)).fetchone()
        return (r["bonus"], bool(r["wash_skill"])) if r else (0.0, False)

    def get_level(guild_id: int, user_id: int):
        r = db.execute("SELECT level FROM user_settings WHERE guild_id=? AND user_id=?",
                       (guild_id, user_id)).fetchone()
        return r["level"] if r else None

    def fee_for(skill: bool) -> float:
        return WASH_FEE_SKILL if skill else WASH_FEE

    async def drug_autocomplete(interaction: discord.Interaction, current: str):
        names = set(DEFAULT_RATES)
        names.update(r["drug"] for r in db.execute(
            "SELECT DISTINCT drug FROM drug_rates WHERE guild_id=?", (interaction.guild_id,)))
        return [app_commands.Choice(name=n, value=n) for n in sorted(names)
                if current.lower() in n][:25]

    # ------------------------------------------------------------ /settings
    @bot.tree.command(description="Save YOUR level, sale bonus and wash skill so /plan needs only your buds")
    @app_commands.guild_only()
    @app_commands.choices(level=LEVEL_CHOICES, bonus=BONUS_CHOICES)
    @app_commands.describe(level="Your drug level",
                           bonus="Bonus % on money per sale",
                           wash_skill="Do you have the wash skill? (16% fee instead of 20%)")
    async def settings(i: discord.Interaction, level: app_commands.Choice[int] | None = None,
                       bonus: app_commands.Choice[int] | None = None,
                       wash_skill: bool | None = None):
        cur_bonus, cur_skill = get_settings(i.guild_id, i.user.id)
        new_bonus = cur_bonus if bonus is None else bonus.value
        new_skill = cur_skill if wash_skill is None else wash_skill
        new_level = get_level(i.guild_id, i.user.id) if level is None else level.value
        db.execute("INSERT INTO user_settings(guild_id,user_id,bonus,wash_skill,level) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(guild_id,user_id) DO UPDATE SET bonus=excluded.bonus, "
                   "wash_skill=excluded.wash_skill, level=excluded.level",
                   (i.guild_id, i.user.id, new_bonus, int(new_skill), new_level))
        db.commit()
        await i.response.send_message(
            f"⚙️ Saved for {i.user.mention}: level **{new_level if new_level else 'not set'}**, sale bonus "
            f"**{new_bonus:g}%**, wash skill **{'yes' if new_skill else 'no'}** "
            f"(wash fee {fee_for(new_skill) * 100:g}%)", ephemeral=True)

    # ------------------------------------------------------------ /drug admin
    drug_group = app_commands.Group(name="drug", description="Drug levels and crafting recipes",
                                    guild_only=True)

    @drug_group.command(name="rate", description="Set the per-bag price range for a drug level (Manage Server)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(drug=drug_autocomplete)
    async def drug_rate(i: discord.Interaction, drug: str, level: app_commands.Range[int, 1, 100],
                        min_price: app_commands.Range[float, 0, 10_000_000],
                        max_price: app_commands.Range[float, 0, 10_000_000]):
        if min_price > max_price:
            return await i.response.send_message("Min price can't be higher than max price.", ephemeral=True)
        drug = norm(drug)
        # First custom rate for a built-in drug: copy the defaults so nothing is lost.
        if not db.execute("SELECT 1 FROM drug_rates WHERE guild_id=? AND drug=?", (i.guild_id, drug)).fetchone():
            for lv, (lo, hi) in DEFAULT_RATES.get(drug, {}).items():
                db.execute("INSERT INTO drug_rates VALUES (?,?,?,?,?)", (i.guild_id, drug, lv, lo, hi))
        db.execute("INSERT INTO drug_rates VALUES (?,?,?,?,?) ON CONFLICT(guild_id,drug,level) "
                   "DO UPDATE SET min_price=excluded.min_price, max_price=excluded.max_price",
                   (i.guild_id, drug, level, min_price, max_price))
        db.commit()
        await i.response.send_message(f"✅ **{drug} L{level}**: {money(min_price)} – {money(max_price)} per bag")

    @drug_group.command(name="recipe", description="Set crafting ratios for a drug (Manage Server)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(drug=drug_autocomplete)
    @app_commands.describe(buds_per_joint="Buds needed per joint", joints_per_bag="Joints needed per bag")
    async def drug_recipe(i: discord.Interaction, drug: str, buds_per_joint: app_commands.Range[int, 1, 1000],
                          joints_per_bag: app_commands.Range[int, 1, 1000]):
        drug = norm(drug)
        db.execute("INSERT INTO drug_recipe VALUES (?,?,?,?) ON CONFLICT(guild_id,drug) "
                   "DO UPDATE SET buds_per_joint=excluded.buds_per_joint, joints_per_bag=excluded.joints_per_bag",
                   (i.guild_id, drug, buds_per_joint, joints_per_bag))
        db.commit()
        await i.response.send_message(f"✅ **{drug}**: {buds_per_joint} bud(s) = 1 joint, "
                                      f"{joints_per_bag} joint(s) = 1 bag "
                                      f"({buds_per_joint * joints_per_bag} buds per bag)")

    @drug_group.command(name="rates", description="Show price ranges per level for a drug")
    @app_commands.autocomplete(drug=drug_autocomplete)
    async def drug_rates(i: discord.Interaction, drug: str = "weed", max_level: app_commands.Range[int, 1, 100] = 10):
        drug = norm(drug)
        rates = get_rates(i.guild_id, drug)
        if not rates:
            return await i.response.send_message(f"No rates for `{drug}`. Add some with `/drug rate`.",
                                                 ephemeral=True)
        lines = []
        for lv in range(1, max_level + 1):
            lo, hi, est = rate_for(rates, lv)
            lines.append(f"`L{lv:<2}` {money(round(lo))} – {money(round(hi))}" + (" *(est.)*" if est else ""))
        bpj, jpb = get_recipe(i.guild_id, drug)
        e = discord.Embed(title=f"📈 {drug} price per bag", description="\n".join(lines), color=0x2ecc71)
        e.set_footer(text=f"Recipe: {bpj} bud(s)=1 joint, {jpb} joint(s)=1 bag • (est.) = interpolated, "
                          f"set exact values with /drug rate")
        await i.response.send_message(embed=e)

    bot.tree.add_command(drug_group)

    # ------------------------------------------------------------ /plan
    @bot.tree.command(description="Buds -> joints -> bags -> dirty & clean money calculator")
    @app_commands.guild_only()
    @app_commands.autocomplete(drug=drug_autocomplete)
    @app_commands.choices(level=LEVEL_CHOICES, bonus=BONUS_CHOICES)
    @app_commands.describe(buds="How many buds you have (use this OR bags)",
                           bags="How many bags you'll sell (use this OR buds)",
                           drug="Drug (default: weed)",
                           level="Your level (default: the one saved with /settings)",
                           bonus="Override your saved sale bonus for this calc",
                           wash_skill="Override your saved wash skill for this calc")
    async def plan(i: discord.Interaction,
                   buds: app_commands.Range[int, 0, 100_000_000] | None = None,
                   bags: app_commands.Range[int, 0, 100_000_000] | None = None,
                   drug: str = "weed", level: app_commands.Choice[int] | None = None,
                   bonus: app_commands.Choice[int] | None = None, wash_skill: bool | None = None):
        if (buds is None) == (bags is None):
            return await i.response.send_message("Give me **either** `buds` **or** `bags` (not both).",
                                                 ephemeral=True)
        level = level.value if level is not None else get_level(i.guild_id, i.user.id)
        bonus = bonus.value if bonus is not None else None
        if level is None:
            level = get_level(i.guild_id, i.user.id)
        if level is None:
            return await i.response.send_message(
                "I don't know your level yet. Add `level:` to this command, or save it once with "
                "`/settings level:10 bonus:20 wash_skill:True`.", ephemeral=True)
        drug = norm(drug)
        r = rate_for(get_rates(i.guild_id, drug), level)
        if r is None:
            return await i.response.send_message(f"No rates for `{drug}` yet. Add some with `/drug rate`.",
                                                 ephemeral=True)
        lo, hi, est = r
        my_bonus, my_skill = get_settings(i.guild_id, i.user.id)
        b = my_bonus if bonus is None else bonus
        skill = my_skill if wash_skill is None else wash_skill
        fee = fee_for(skill)
        bpj, jpb = get_recipe(i.guild_id, drug)
        buds_per_bag = bpj * jpb

        e = discord.Embed(title=f"🧮 {drug} L{level}", color=0x9b59b6)
        if buds is not None:
            joints = buds // bpj
            n_bags = joints // jpb
            leftover_joints = joints % jpb
            leftover_buds = buds % bpj
            e.add_field(name="🌿 Crafting", inline=False, value=(
                f"{buds:,} buds → {joints:,} joints → **{n_bags:,} bags**\n"
                f"Leftover: {leftover_joints:,} joint(s) + {leftover_buds:,} bud(s)"))
        else:
            n_bags = bags
            e.add_field(name="🌿 Crafting", inline=False, value=(
                f"{n_bags:,} bags needs {n_bags * jpb:,} joints = **{n_bags * buds_per_bag:,} buds**"))

        mult = 1 + b / 100
        d_lo, d_hi = lo * n_bags * mult, hi * n_bags * mult
        d_avg = (d_lo + d_hi) / 2
        c_lo, c_hi, c_avg = (x * (1 - fee) for x in (d_lo, d_hi, d_avg))
        e.add_field(name="💵 Dirty money", inline=False, value=(
            f"Min {money(round(d_lo))} • Avg {money(round(d_avg))} • Max {money(round(d_hi))}"))
        e.add_field(name="🧼 Clean money", inline=False, value=(
            f"Min {money(round(c_lo))} • Avg {money(round(c_avg))} • Max {money(round(c_hi))}\n"
            f"Wash lost: {money(round(d_avg - c_avg))} avg"))
        e.set_footer(text=(
            f"Per bag: {money(round(lo * mult))}–{money(round(hi * mult))} "
            f"(base {money(round(lo))}–{money(round(hi))}) • bonus {b:g}% • wash fee {fee * 100:g}%"
            + (" • level price is an estimate" if est else "")))
        await i.response.send_message(embed=e)

    # ------------------------------------------------------------ /wash
    @bot.tree.command(description="How much clean money you get from washing dirty money")
    @app_commands.guild_only()
    @app_commands.describe(dirty="Dirty money amount", wash_skill="Override your wash skill for this calc")
    async def wash(i: discord.Interaction, dirty: app_commands.Range[float, 0, 1_000_000_000],
                   wash_skill: bool | None = None):
        _, my_skill = get_settings(i.guild_id, i.user.id)
        skill = my_skill if wash_skill is None else wash_skill
        fee = fee_for(skill)
        clean = dirty * (1 - fee)
        await i.response.send_message(
            f"🧼 {money(dirty)} dirty → **{money(round(clean))}** clean "
            f"(fee {fee * 100:g}% = {money(round(dirty - clean))}"
            f"{', wash skill' if skill else ''})")

    # ------------------------------------------------------------ /sold
    @bot.tree.command(description="Log a real drug sale with the actual dirty money you received")
    @app_commands.guild_only()
    @app_commands.autocomplete(drug=drug_autocomplete)
    @app_commands.choices(level=LEVEL_CHOICES)
    @app_commands.describe(level="Level of the drug you sold", bags="Bags sold",
                           dirty="Total dirty money you received", note="Buyer / location / anything")
    async def sold(i: discord.Interaction, drug: str, level: app_commands.Choice[int],
                   bags: app_commands.Range[int, 1, 1_000_000],
                   dirty: app_commands.Range[float, 0, 1_000_000_000], note: str | None = None):
        level = level.value
        drug = norm(drug)
        _, skill = get_settings(i.guild_id, i.user.id)
        db.execute("INSERT INTO sales(guild_id,user_id,item,qty,unit_cost,unit_price,note,ts) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (i.guild_id, i.user.id, f"{drug} L{level}", bags, 0, dirty / bags, note, int(time.time())))
        db.commit()
        clean = dirty * (1 - fee_for(skill))
        await i.response.send_message(
            f"💰 {i.user.mention} sold **{bags:,}× {drug} L{level}** for {money(dirty)} dirty "
            f"({money(round(dirty / bags))}/bag) → ≈ {money(round(clean))} clean after washing"
            + (f"\n> {note}" if note else ""))
