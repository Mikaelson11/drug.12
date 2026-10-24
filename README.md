# GTA RP Sales Tracker (Discord bot)

Tracks in-game item sales, costs, expenses and profit for your RP crew. All data is stored per server in a local SQLite file.

## Setup

1. Go to https://discord.com/developers/applications → **New Application** → **Bot** → copy the token.
2. Invite the bot: **OAuth2 → URL Generator**, tick scopes `bot` + `applications.commands`, permission `Send Messages` + `Embed Links`.
3. Install and run (Python 3.10+):

```bash
pip install -r requirements.txt
cp .env.example .env     # then paste your token into .env
python bot.py
```

Slash commands register automatically on startup (global commands can take a few minutes to show up the first time).

## Commands

| Command | What it does |
|---|---|
| `/item set name cost price` | Add/update an item (Manage Server) |
| `/item list` / `/item remove` | View or delete items |
| `/calc item qty [price] [cost]` | Quick profit calculator, saves nothing |
| `/sale item qty [price] [note]` | Log a sale (uses list price unless overridden) |
| `/expense amount [note]` | Log supplies, fines, vehicle costs, etc. |
| `/undo` | Remove your last sale |
| `/stats [period] [user]` | Revenue, costs, expenses, net profit, top items |
| `/leaderboard [period]` | Top earners |
| `/history [user]` | Last 10 sales |
| `/split people [period] [cut]` | Split net profit, optionally holding back a % for the crew fund |
| `/reset confirm:RESET` | Wipe sales and expenses (Manage Server) |

## MWRP drug calculator

Weed ships pre-loaded: L1 = $648–852/bag, L10 = $780–1006/bag, 1 bud = 1 joint, 2 joints = 1 bag. Wash fee is 20%, or 16% with the wash skill. Only bags can be sold.

| Command | What it does |
|---|---|
| `/settings [level] [bonus] [wash_skill]` | Save YOUR level, sale bonus and wash skill once. All three are dropdowns (levels 1-10, bonus 0-50%, wash skill True/False) |
| `/plan buds:1000` | **One command for everything:** 1000 buds → 1000 joints → 500 bags, then min/avg/max dirty money after the sale and clean money after washing. Uses weed and your saved settings |
| `/plan bags:500` | How many buds you need for that many bags, plus dirty/clean money |
| `/plan buds:1000 level:5 bonus:0 drug:weed` | Same, but override any saved setting for one calc |
| `/wash dirty` | Clean money after the wash fee |
| `/sold drug level bags dirty [note]` | Log a real sale with the dirty money you got (shows up in `/stats`, `/leaderboard`, `/history`) |
| `/drug rates [drug]` | Show prices for levels 1-10 |
| `/drug rate drug level min max` | Set the exact price range for a level (Manage Server) |
| `/drug recipe drug buds_per_joint joints_per_bag` | Change crafting ratios, or add other drugs (Manage Server) |

Levels between the ones you've set (e.g. L2–L9 for weed) are **estimated** by a straight line between the nearest known levels and marked "(est.)". Fill in the exact numbers with `/drug rate` as you find them.

Typical flow: set up your items once with `/item set`, then `/sale` after each deal and `/stats` or `/split` at the end of the week.
