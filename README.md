# BiwengerBot

A helper bot for a [Biwenger](https://biwenger.as.com) fantasy football league (LaLiga). Every day it scrapes the league, stores the history, and sends a Telegram digest with what to do before the next matchweek: which players to bid on and how much, what to sell, flip ideas, the best XI and captain, and a warning when the balance would be negative at the lock. After each matchweek it also reviews what went wrong and writes the lessons down.

It only gives advice. It never bids, buys or sells on its own.

## What runs every day

The **Daily scrape & migrate** GitHub Action (`.github/workflows/scrape-and-migrate.yml`) runs at 06:15 UTC:

1. **`scraper/scraper.py`** logs in with Playwright. It saves the market, every team, standings, bids, offers, lineups, round scores, and the next matchweek's start time (`csvs/others/next_round.json`). It also renews players whose clause is about to expire.
2. **`migration.py`** loads the CSVs into the SQLite database `data/biwenger_data.db`.
3. **`notify.py`** builds the Telegram digest (see below). It skips cleanly if the Telegram secrets aren't set.
4. **`export_static.py`** freezes the dashboard into `docs/` for GitHub Pages.
5. All changed tracked files are committed back to `main`.

A second workflow, `scrape-probabilities.yml`, updates players' start probabilities on Monday, Wednesday and Friday.

## The Telegram digest

In order:

- **Balance warning**: shows how many days and hours are left until the lock, and whether the balance is negative. If it is, it suggests what to sell to get back above €0. Only certain money counts toward that, such as offers you can accept right now, and flips are sold first. A negative balance at the lock scores the whole round as 0.
- **Matchweek review**: appears once a round finishes. It shows points, rank, and the lessons from that round.
- **Daily check**: best-XI starters who might not play, plus the suggested captain.
- **Buy**: players worth bidding on, each with an "even odds" bid and a "safe" bid.
- **Sell**: candidates, with how each computer offer compares to market value.
- **Flips**: buys made only for profit. They use spare money only, are timed to the days left before the lock, and are always sold before any starter.

## How the advice works (`recommenders.py`)

- **Bid model**: auctions are first-price sealed bid, so the winner pays exactly what they bid. Suggested bids come from what real winning bids were over the last 45 days, compared with the asking price. They are grouped by the player's price trend that day:
  - **Falling** players usually have one bidder and go for about asking price.
  - **Hot risers** draw bidding wars.
  - **Stars on a dip** (over €6M and falling) use the price-band numbers instead, because rivals may still fight over them.
- **Best XI**: picks the best valid formation from your own squad. The captain and the ariete (striker slot) must each cost under €5M.
- **Flips**: estimate how much a rising player keeps rising over the next few days, based on past price moves. A flip is suggested only if that rise beats the overpay.

## Learning from each matchweek (`review.py`)

For each finished round, it compares your score with the league and with the best you could have done using your own players. It checks:

- bench and formation choices
- the captain
- starters who didn't play
- a zeroed round
- bids lost by less than 5%

Results are appended to `csvs/others/round_reviews.csv` and `lessons.md`. Lessons that come back in 2 of the last 5 rounds are flagged in the digest, and the daily check gets stricter for them.

Run it by hand:

```bash
python review.py         # review any newly finished rounds
python review.py 4911    # print one round (round number = id - 4898)
```

## Dashboard

`app.py` is a small Flask dashboard (deployable with `render.yaml`). The daily Action also exports a static copy to `docs/`, which is served by GitHub Pages.

## Running locally

```bash
pip install -r requirements.txt
python -m playwright install chromium
export BIWENGER_EMAIL=... BIWENGER_PASSWORD=...
python scraper/scraper.py && python migration.py
python notify.py           # prints the digest if Telegram isn't set up
python analysis/backtest_bid_model.py   # check the bid model against past auctions
```

## Secrets

Set these under **Settings → Secrets and variables → Actions**. Never commit them.

| Secret | What it is |
|---|---|
| `BIWENGER_EMAIL`, `BIWENGER_PASSWORD` | Biwenger login used by the scraper |
| `TELEGRAM_BOT_TOKEN` | From @BotFather when you create the bot |
| `TELEGRAM_CHAT_ID` | Your chat id, from `https://api.telegram.org/bot<TOKEN>/getUpdates` after messaging the bot |

## Ground rules

- Changes go on a feature branch and are merged into `main` by fast-forward.
- Be careful with anything that rewrites the database or CSVs. History files such as `round_reviews.csv` and `round_locks.csv` are only ever added to, never rewritten.
- Any future auto-trading needs a dry-run mode and a confirm step first.
