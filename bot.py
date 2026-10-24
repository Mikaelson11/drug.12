"""
GTA RP Sales Tracker - Discord bot
Tracks in-game (roleplay) item sales, costs, expenses and profit per server.
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

# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS items (
        guild_id INTEGER NOT NULL,
        name     TEXT    NOT NULL,
        cost     REAL    NOT NULL,   -- what it costs you per unit
        price    REAL    NOT NULL,   -- default sell price per unit
        PRIMARY KEY (guild_id, name)
    );
    CREATE TABLE IF NOT EXISTS sales (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        item       TEXT    NOT NULL,
        qty        INTEGER NOT NULL,
        unit_cost  REAL    NOT NULL,
        unit_price REAL    NOT NULL,
        note       TEXT,
        ts         INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS expenses (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        amount   REAL    NOT NULL,
        note     TEXT,
        ts       INTEGER NOT NULL
    );
    """
)
db.commit()


def money(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def period_start(period: str) -> int:
    now = int(time.time())
    return {"today": now - now % 86400, "week": now - 7 * 86400,
            "month": now - 30 * 86400}.get(period, 0)


# ---------------------------------------------------------------- bot setup
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = Bot()
PERIODS = [app_commands.Choice(name=n, value=v) for n, v in
           [("Today", "today"), ("Last 7 days", "week"),
            ("Last 30 days", "month"), ("All time", "all")]]


async def item_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.execute(
        "SELECT name FROM items WHERE guild_id=? AND name LIKE ? LIMIT 25",
        (interaction.guild_id, f"%{current.lower()}%"),
    ).fetchall()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]


def get_item(guild_id: int, name: str):
    return db.execute("SELECT * FROM items WHERE guild_id=? AND name=?",
                      (guild_id, name.lower().strip())).fetchone()


# ---------------------------------------------------------------- item setup
item_group = app_commands.Group(name="item", description="Manage your item price list",
                                guild_only=True)


@item_group.command(name="set", description="Add or update an item (needs Manage Server)")
@app_commands.describe(name="Item name", cost="Your cost per unit", price="Default sell price per unit")
@app_commands.default_permissions(manage_guild=True)
async def item_set(i: discord.Interaction, name: str, cost: app_commands.Range[float, 0, 10_000_000],
                   price: app_commands.Range[float, 0, 10_000_000]):
    db.execute(
        "INSERT INTO items VALUES (?,?,?,?) ON CONFLICT(guild_id,name) "
        "DO UPDATE SET cost=excluded.cost, price=excluded.price",
        (i.guild_id, name.lower().strip(), cost, price),
    )
    db.commit()
    await i.response.send_message(
        f"✅ **{name.lower().strip()}** saved: cost {money(cost)}, sell {money(price)} "
        f"(profit {money(price - cost)}/unit)")


@item_group.command(name="remove", description="Remove an item (needs Manage Server)")
@app_commands.default_permissions(manage_guild=True)
@app_commands.autocomplete(name=item_autocomplete)
async def item_remove(i: discord.Interaction, name: str):
    cur = db.execute("DELETE FROM items WHERE guild_id=? AND name=?",
                     (i.guild_id, name.lower().strip()))
    db.commit()
    await i.response.send_message("🗑️ Removed." if cur.rowcount else "Item not found.",
                                  ephemeral=not cur.rowcount)


@item_group.command(name="list", description="Show the price list")
async def item_list(i: discord.Interaction):
    rows = db.execute("SELECT * FROM items WHERE guild_id=? ORDER BY name", (i.guild_id,)).fetchall()
    if not rows:
        return await i.response.send_message("No items yet. Use `/item set` to add some.", ephemeral=True)
    lines = [f"`{r['name']}` — cost {money(r['cost'])} | sell {money(r['price'])} | "
             f"profit {money(r['price'] - r['cost'])}" for r in rows]
    await i.response.send_message(embed=discord.Embed(title="📋 Price list",
                                                      description="\n".join(lines), color=0x2ecc71))


bot.tree.add_command(item_group)


# ---------------------------------------------------------------- calculator
@bot.tree.command(description="Calculate cost, revenue and profit for a batch (nothing is saved)")
@app_commands.guild_only()
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.describe(item="Item", qty="How many units", price="Override sell price per unit",
                       cost="Override cost per unit")
async def calc(i: discord.Interaction, item: str, qty: app_commands.Range[int, 1, 1_000_000],
               price: float | None = None, cost: float | None = None):
    row = get_item(i.guild_id, item)
    if not row and (price is None or cost is None):
        return await i.response.send_message(
            "Unknown item. Add it with `/item set`, or pass both `price` and `cost`.", ephemeral=True)
    unit_cost = cost if cost is not None else row["cost"]
    unit_price = price if price is not None else row["price"]
    total_cost, revenue = unit_cost * qty, unit_price * qty
    profit = revenue - total_cost
    margin = (profit / revenue * 100) if revenue else 0
    e = discord.Embed(title=f"🧮 {qty:,}× {item.lower()}", color=0x3498db)
    e.add_field(name="Total cost", value=money(total_cost))
    e.add_field(name="Revenue", value=money(revenue))
    e.add_field(name="Profit", value=f"{money(profit)} ({margin:.1f}%)")
    await i.response.send_message(embed=e)


# ---------------------------------------------------------------- logging
@bot.tree.command(description="Log a sale")
@app_commands.guild_only()
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.describe(item="Item sold", qty="Units sold", price="Sell price per unit (defaults to list price)",
                       note="Buyer / location / anything")
async def sale(i: discord.Interaction, item: str, qty: app_commands.Range[int, 1, 1_000_000],
               price: float | None = None, note: str | None = None):
    row = get_item(i.guild_id, item)
    if not row:
        return await i.response.send_message("Unknown item. Add it with `/item set` first.", ephemeral=True)
    unit_price = price if price is not None else row["price"]
    db.execute("INSERT INTO sales(guild_id,user_id,item,qty,unit_cost,unit_price,note,ts) VALUES (?,?,?,?,?,?,?,?)",
               (i.guild_id, i.user.id, row["name"], qty, row["cost"], unit_price, note, int(time.time())))
    db.commit()
    profit = (unit_price - row["cost"]) * qty
    await i.response.send_message(
        f"💰 {i.user.mention} sold **{qty:,}× {row['name']}** @ {money(unit_price)} = "
        f"**{money(unit_price * qty)}** (profit {money(profit)})" + (f"\n> {note}" if note else ""))


@bot.tree.command(description="Log an expense (supplies, bribes, vehicle, fines...)")
@app_commands.guild_only()
async def expense(i: discord.Interaction, amount: app_commands.Range[float, 0.01, 10_000_000],
                  note: str | None = None):
    db.execute("INSERT INTO expenses(guild_id,user_id,amount,note,ts) VALUES (?,?,?,?,?)",
               (i.guild_id, i.user.id, amount, note, int(time.time())))
    db.commit()
    await i.response.send_message(f"🧾 Logged expense {money(amount)}" + (f" — {note}" if note else ""))


@bot.tree.command(description="Delete your most recent sale")
@app_commands.guild_only()
async def undo(i: discord.Interaction):
    row = db.execute("SELECT * FROM sales WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                     (i.guild_id, i.user.id)).fetchone()
    if not row:
        return await i.response.send_message("You have no sales to undo.", ephemeral=True)
    db.execute("DELETE FROM sales WHERE id=?", (row["id"],))
    db.commit()
    await i.response.send_message(f"↩️ Removed your sale of {row['qty']}× {row['item']}.")


# ---------------------------------------------------------------- reporting
def totals(guild_id: int, since: int, user_id: int | None = None):
    q, args = "FROM sales WHERE guild_id=? AND ts>=?", [guild_id, since]
    eq, eargs = "FROM expenses WHERE guild_id=? AND ts>=?", [guild_id, since]
    if user_id:
        q += " AND user_id=?"; args.append(user_id)
        eq += " AND user_id=?"; eargs.append(user_id)
    s = db.execute(f"SELECT COALESCE(SUM(qty*unit_price),0) rev, COALESCE(SUM(qty*unit_cost),0) cost, "
                   f"COALESCE(SUM(qty),0) units, COUNT(*) n {q}", args).fetchone()
    ex = db.execute(f"SELECT COALESCE(SUM(amount),0) total {eq}", eargs).fetchone()["total"]
    return s, ex


@bot.tree.command(description="Show sales stats")
@app_commands.guild_only()
@app_commands.choices(period=PERIODS)
@app_commands.describe(period="Time range", user="Only this person (default: whole crew)")
async def stats(i: discord.Interaction, period: app_commands.Choice[str] = None, user: discord.Member | None = None):
    p = period.value if period else "week"
    s, ex = totals(i.guild_id, period_start(p), user.id if user else None)
    gross = s["rev"] - s["cost"]
    net = gross - ex
    e = discord.Embed(title=f"📊 {'Crew' if not user else user.display_name} — {p}", color=0xf1c40f)
    e.add_field(name="Sales", value=f"{s['n']:,} ({s['units']:,} units)")
    e.add_field(name="Revenue", value=money(s["rev"]))
    e.add_field(name="Item costs", value=money(s["cost"]))
    e.add_field(name="Expenses", value=money(ex))
    e.add_field(name="Gross profit", value=money(gross))
    e.add_field(name="**Net profit**", value=f"**{money(net)}**")
    top = db.execute("SELECT item, SUM(qty) q, SUM(qty*(unit_price-unit_cost)) p FROM sales "
                     "WHERE guild_id=? AND ts>=? GROUP BY item ORDER BY p DESC LIMIT 5",
                     (i.guild_id, period_start(p))).fetchall()
    if top:
        e.add_field(name="Top items (by profit)", inline=False,
                    value="\n".join(f"`{r['item']}` — {r['q']:,} sold, {money(r['p'])}" for r in top))
    await i.response.send_message(embed=e)


@bot.tree.command(description="Profit leaderboard")
@app_commands.guild_only()
@app_commands.choices(period=PERIODS)
async def leaderboard(i: discord.Interaction, period: app_commands.Choice[str] = None):
    p = period.value if period else "week"
    rows = db.execute("SELECT user_id, SUM(qty*(unit_price-unit_cost)) profit, SUM(qty) units FROM sales "
                      "WHERE guild_id=? AND ts>=? GROUP BY user_id ORDER BY profit DESC LIMIT 10",
                      (i.guild_id, period_start(p))).fetchall()
    if not rows:
        return await i.response.send_message("No sales in that period.", ephemeral=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[n] if n < 3 else f'`{n+1}.`'} <@{r['user_id']}> — {money(r['profit'])} "
             f"({r['units']:,} units)" for n, r in enumerate(rows)]
    await i.response.send_message(embed=discord.Embed(title=f"🏆 Leaderboard — {p}",
                                                      description="\n".join(lines), color=0xe67e22))


@bot.tree.command(description="Show recent sales")
@app_commands.guild_only()
async def history(i: discord.Interaction, user: discord.Member | None = None):
    uid = (user or i.user).id
    rows = db.execute("SELECT * FROM sales WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                      (i.guild_id, uid)).fetchall()
    if not rows:
        return await i.response.send_message("No sales found.", ephemeral=True)
    lines = [f"<t:{r['ts']}:d> {r['qty']:,}× `{r['item']}` @ {money(r['unit_price'])} → "
             f"{money(r['qty'] * (r['unit_price'] - r['unit_cost']))} profit" for r in rows]
    await i.response.send_message(embed=discord.Embed(description="\n".join(lines), color=0x95a5a6,
                                                      title=f"Recent sales — <@{uid}>".replace(f"<@{uid}>", (user or i.user).display_name)))


@bot.tree.command(description="Split the crew's net profit between members")
@app_commands.guild_only()
@app_commands.choices(period=PERIODS)
@app_commands.describe(people="Number of people splitting", cut="Percent held back for the boss/crew fund (default 0)")
async def split(i: discord.Interaction, people: app_commands.Range[int, 1, 100],
                period: app_commands.Choice[str] = None, cut: app_commands.Range[float, 0, 100] = 0):
    p = period.value if period else "week"
    s, ex = totals(i.guild_id, period_start(p))
    net = s["rev"] - s["cost"] - ex
    if net <= 0:
        return await i.response.send_message(f"Net profit for {p} is {money(net)} — nothing to split.")
    held = net * cut / 100
    each = (net - held) / people
    await i.response.send_message(
        f"💸 **Net profit ({p}):** {money(net)}\n"
        f"Held back ({cut:g}%): {money(held)}\n"
        f"**Each of {people}: {money(each)}**")


@bot.tree.command(description="Wipe all sales/expenses for this server (needs Manage Server)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def reset(i: discord.Interaction, confirm: str):
    if confirm != "RESET":
        return await i.response.send_message("Type `RESET` in the confirm field to wipe all sales and expenses.",
                                             ephemeral=True)
    db.execute("DELETE FROM sales WHERE guild_id=?", (i.guild_id,))
    db.execute("DELETE FROM expenses WHERE guild_id=?", (i.guild_id,))
    db.commit()
    await i.response.send_message("🧹 All sales and expenses cleared. Item prices kept.")


import mwrp  # noqa: E402  (MWRP drug levels / bonus / wash / crafting calculator)

mwrp.setup(bot, db, money)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file (see .env.example).")
    bot.run(TOKEN)
