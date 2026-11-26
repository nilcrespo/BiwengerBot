"""Sends a daily Telegram digest: buy/sell recommendations (via
recommenders.py, the same logic the dashboard uses) plus the outcome of
scraper.py's auto-renewal step, if it ran. Run after migration.py in the
daily GitHub Action, once the DB reflects today's scrape.

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as env vars — see
README/setup notes for how to obtain them. Missing/unset is treated as
"not configured yet", not an error: this prints a warning and exits
cleanly rather than failing the workflow.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

import pandas as pd

import recommenders

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RENEWAL_RESULTS_PATH = "csvs/others/renewal_results.json"
MADRID_TZ = "Europe/Madrid"


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping notification")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status == 200
        print("✅ Telegram message sent" if ok else f"⚠️ Telegram send returned HTTP {resp.status}")
        return ok
    except Exception as e:
        print(f"⚠️ Telegram send failed: {e}")
        return False


def _money(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if n < 0 else ""
    return f"{sign}€{abs(n):,.0f}"


def _load_next_round(now=None):
    return recommenders.load_next_round(now)


def _when(info) -> str:
    kickoff = pd.Timestamp(info["start"], unit="s", tz=MADRID_TZ)
    days = info["days_left"]
    if days < 1:
        left = f"in {days * 24:.0f} h"
    else:
        left = f"in {days:.1f} days"
    name = info.get("round_name") or "Next round"
    return f"{name} locks {kickoff:%a %d %b, %H:%M} ({left})"


def _funding_sells(sell_df, shortfall, flips_df=None):
    """The sell recommender's own top picks, taken in its ranking order
    until their sale value covers the shortfall. Uses effective_price, the
    computer's instant offer when there is one, since that's money that
    lands before the deadline for certain."""
    picks, total = [], 0.0
    # Flip players go first, always: they were bought to be sold, so
    # covering a shortfall should never cost a starter while one is held.
    for _, r in (flips_df if flips_df is not None else pd.DataFrame()).iterrows():
        if total >= shortfall:
            break
        value = r.get("sale_value")
        if value is None or pd.isna(value) or value <= 0:
            continue
        picks.append((f"{r['name']} [flip]", float(value), pd.notna(r.get("offer_price"))))
        total += float(value)
    if sell_df is None or not len(sell_df):
        return picks, total
    taken = {p[0].replace(" [flip]", "") for p in picks}
    for _, r in sell_df.iterrows():
        if r["player"] in taken:
            continue
        if total >= shortfall:
            break
        value = r.get("effective_price")
        if value is None or pd.isna(value) or value <= 0:
            continue
        picks.append((r["player"], float(value), bool(r.get("has_offer"))))
        total += float(value)
    return picks, total


def _balance_section(conn, date, sell_df=None, now=None, flips_df=None) -> str:
    """Countdown to the next matchweek lock, plus a loud warning when the
    live balance is negative.

    Biwenger needs a non-negative balance when a round starts, or the
    whole round scores zero (see recommenders.get_my_balance). That cost
    us round 10, so a negative balance always comes with the deadline and
    a concrete list of sells that would cover it. A healthy balance still
    gets the one-line countdown, so the deadline is never a surprise."""
    balance = recommenders.get_my_balance(conn, date)
    info = _load_next_round(now)
    when = _when(info) if info else None

    if balance >= 0:
        if not when:
            return ""
        return f"📅 {when}\n  Balance {_money(balance)} ✅"

    shortfall = -balance
    lines = [f"🚨 <b>Balance is negative: {_money(balance)}</b>"]
    if when:
        lines.append(f"  {when}. If it's still negative then, the whole round scores 0.")
    else:
        lines.append("  Biwenger needs a non-negative balance before the next round, "
                     "or the round scores 0.")
    picks, total = _funding_sells(sell_df, shortfall, flips_df)
    if picks and total >= shortfall:
        parts = [f"{name} ({_money(value)}{', instant offer' if offer else ''})"
                 for name, value, offer in picks]
        lines.append(f"  To cover {_money(shortfall)}, sell: " + ", ".join(parts))
    elif picks:
        lines.append(f"  The sell list only covers {_money(total)} of the {_money(shortfall)} needed. "
                     "You'll need to sell someone outside it.")
    return "\n".join(lines)


def _renewal_section() -> str:
    """One line confirming the auto-renew step ran and succeeded — cheap
    peace of mind that listings aren't quietly lapsing, distinct from the
    buy/sell sections below which only appear when there's something
    worth acting on."""
    if not os.path.exists(RENEWAL_RESULTS_PATH):
        return ""
    try:
        with open(RENEWAL_RESULTS_PATH) as f:
            results = json.load(f)
    except Exception:
        return ""
    if not results:
        return ""

    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    line = f"🔄 <b>Renewed {len(ok)}/{len(results)} listings</b>"
    if failed:
        names = ", ".join(r.get("player_name") or str(r.get("player_id")) for r in failed[:5])
        line += f"\n  ⚠️ Failed: {names}"
    return line


def _log_flip_plan(plan, date):
    """Append today's flip suggestions to the flip log, so a player bought
    off this list is later recognized as a flip (sold first, tracked
    separately). One row per player per day; re-running a day replaces
    that day's rows instead of duplicating them."""
    path = recommenders.FLIP_LOG_PATH
    cols = ["date", "name", "club", "price", "bid", "expected_profit", "round_name"]
    try:
        log = pd.read_csv(path)
    except Exception:
        log = pd.DataFrame(columns=cols)
    log = log[log["date"] != date]
    if len(plan):
        today = plan.assign(date=date)[cols]
        log = pd.concat([log, today], ignore_index=True)
    log[cols].to_csv(path, index=False)


def _flip_section(conn, date, plan, note, owned) -> str:
    lines = []
    if len(owned):
        info = recommenders.load_next_round()
        by = f" before {_when(info)}" if info else ""
        lines.append(f"🔁 <b>Your flip players</b> (sell these first{by})")
        for _, r in owned.iterrows():
            src = "computer offer" if pd.notna(r.get("offer_price")) else "market value"
            profit = f", profit {_money(r['profit'])}" if pd.notna(r.get("profit")) else ""
            lines.append(f"  • {r['name']} — {src} {_money(r['sale_value'])}{profit}")
    if len(plan):
        info = recommenders.load_next_round()
        lines.append(f"💰 <b>Flip plan</b> (profit only, spare money only, "
                     f"{plan['horizon_days'].iloc[0]} days to {plan['round_name'].iloc[0] or 'the matchweek'})")
        for _, r in plan.head(5).iterrows():
            lines.append(
                f"  • {r['name']} ({r['club']}) — {_money(r['price'])}, up {r['rise_today_pct']:.1f}% today → "
                f"bid {_money(r['bid'])}, expect about +{_money(r['expected_profit'])} "
                f"({r['expected_profit_pct']:.0f}%) if sold before the lock"
            )
    elif note and not len(owned):
        lines.append(f"💰 No flips today: {note}.")
    return "\n".join(lines)


def build_digest(conn, date) -> str:
    buy_df, sell_df = recommenders.build_recommendations(conn, date)

    sections = []

    flip_plan, flip_note = recommenders.build_flip_plan(conn, date)
    _log_flip_plan(flip_plan, date)
    owned = recommenders.owned_flips(conn, date)
    balance_warning = _balance_section(conn, date, sell_df, flips_df=owned)
    if balance_warning:
        sections.append(balance_warning)

    renewal = _renewal_section()
    if renewal:
        sections.append(renewal)

    if len(buy_df):
        lines = [f"🎯 <b>Buy: {len(buy_df)} real deal(s)</b>"]
        for _, r in buy_df.head(5).iterrows():
            need = f" [{r['squad_need']}]" if r.get('squad_need') else ""
            afford = ""
            if r.get('shortfall', 0) > 0:
                if r.get('funding_plan'):
                    afford = f"\n    ⚠️ short {_money(r['shortfall'])} — would need to sell: {r['funding_plan']}"
                else:
                    afford = f"\n    ⚠️ short {_money(r['shortfall'])}, no sells available to cover it"
            # Biwenger's market is a first-price sealed-bid auction — the
            # winner pays exactly what they bid, never the runner-up's
            # amount plus an increment (confirmed on 9/9 real transactions
            # captured so far). That makes "bid the predicted average
            # price" the wrong advice: every euro above the true minimum
            # needed to clear the field is pure waste. win_bid_50/90 are
            # framed as what they actually are — the minimum bid for a
            # roughly-coin-flip vs. a pretty-safe chance of winning — not
            # a range around an expected price.
            rivals = f", vs {r['top_competitors']}" if r.get('top_competitors') else ""
            # A high buy score only ever judges the incoming player alone.
            # potential_points_gain re-runs Best XI with him swapped in
            # and the funded sells swapped out — a good player on paper
            # can still be a net-zero (or worse) squad upgrade once that
            # actually plays out, and this is the only place that shows.
            gain = r.get('potential_points_gain')
            xi_note = f"\n    📈 Best XI: +{gain:.1f} pts/round net of the sells above" if gain and gain > 0 else ""
            lines.append(
                f"  • {r['name']} ({r['club']}, {r['position']}) — {_money(r['price'])}, "
                f"{r['probability']} start, score {r['score']}{need} → "
                f"bid {_money(r['win_bid_50'])} for even odds, {_money(r['win_bid_90'])} to be safe "
                f"(~{r['expected_bidders']:g} bidders expected{rivals}){afford}{xi_note}"
            )
        sections.append("\n".join(lines))

    if len(sell_df):
        lines = [f"📤 <b>Sell: {len(sell_df)} candidate(s)</b>"]
        for _, r in sell_df.head(5).iterrows():
            tag = ""
            if r.get('offer_is_generous'):
                tag = f" 🔥 offer beats market by {r['offer_premium_pct']:.1f}% — worth grabbing"
            elif r.get('offer_is_lowball'):
                tag = f" ⏳ offer is {abs(r['offer_premium_pct']):.1f}% under market — consider waiting"
            if r.get('momentum_reversal'):
                tag += f" 📉 was +{r['trend_pct_window']:.1f}% but turned down today — lock it in"
            profit = _money(r['profit']) if pd.notna(r['profit']) else "—"
            lines.append(f"  • {r['player']} ({r['club']}) — profit {profit}, score {r['score']}{tag}")
        sections.append("\n".join(lines))

    # Profit-only buys, kept apart from the buy list above: they're judged
    # on resale before the matchweek, never on points (see the flip rules
    # in recommenders.py).
    flips = _flip_section(conn, date, flip_plan, flip_note, owned)
    if flips:
        sections.append(flips)

    return "\n\n".join(sections)


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    conn = sqlite3.connect('data/biwenger_data.db')
    if not date:
        row = conn.execute("SELECT MAX(scraped_at) FROM market").fetchone()
        date = (row[0] or "")[:10]
    if not date:
        print("No scraped data yet — nothing to notify about")
        conn.close()
        return

    digest = build_digest(conn, date)
    conn.close()

    if not digest:
        print("Nothing noteworthy today — skipping Telegram message")
        return

    send_telegram_message(f"⚽ <b>Biwenger digest — {date}</b>\n\n{digest}")


if __name__ == "__main__":
    main()
