"""Backtest the bid-competition model against real completed auctions.

Run from the repo root:  python analysis/backtest_bid_model.py

Answers three questions, in order of how much weight they deserve:

1. On every real transaction we have full bid data for, what would the
   new model have predicted for the winning bid, using only what was
   known BEFORE that transaction — versus the old flat bucket markup,
   versus what actually happened.

2. The same, but letting the model calibrate on the transactions it is
   being scored against. That is in-sample and proves nothing; it is
   here only to show which direction the model moves as the board feed
   accumulates, and it is labelled as such in the output.

3. Sanity checks on the market as it stands right now, including the
   two shapes that motivated all of this: an expensive player whose
   price is sliding (must NOT be handed a big markup) and a cheap one
   rising sharply (must get credit for momentum).

Read the sample size before reading the numbers. The league board is a
shallow rolling window with no paging (see scraper.get_league_board_market),
so this data accumulates one day at a time and started accumulating on
2026-08-20. With a handful of auctions these are anecdotes, not a
validated error rate, and the script says so out loud.
"""
import os
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recommenders  # noqa: E402

DB = 'data/biwenger_data.db'


def money(n):
    if n is None or pd.isna(n):
        return "—"
    return f"€{float(n):,.0f}"


def pct(n):
    if n is None or pd.isna(n):
        return "—"
    return f"{float(n) * 100:+.1f}%"


def old_model_bid(price, change, bucket_avg_bids):
    """The bid suggestion this repo made before the competition model —
    reproduced here verbatim so the comparison is against what actually
    shipped, not a paraphrase of it."""
    avg_bids = bucket_avg_bids.get(recommenders._bucket_label(price))
    bucket_markup = min(avg_bids, 10) * 0.02 if avg_bids else 0.05
    change = change if pd.notna(change) else 0
    if change > 0:
        return round((price * (1 + bucket_markup) + change) / 10_000) * 10_000
    return round(price * (1 + bucket_markup * 0.25) / 10_000) * 10_000


def load_transactions(conn):
    """One row per completed auction, with the asking price bidders would
    have seen reconstructed as (price at capture − that day's own
    increment) — see recommenders._markup_per_bidder for why that
    reconstruction is necessary and what it costs in accuracy."""
    txns = pd.read_sql(
        """
        SELECT DISTINCT txn_key, DATE(scraped_at) AS captured_on, player_name,
               player_position, player_price, price_change, winning_amount, num_bidders
        FROM market_bid_history
        WHERE is_winner = 1
        ORDER BY captured_on
        """,
        conn
    )
    if len(txns):
        txns.loc[:, 'asking'] = txns['player_price'] - txns['price_change'].fillna(0)
    return txns


def runner_up(conn, txn_key):
    row = conn.execute(
        "SELECT bidder_name, bid_amount FROM market_bid_history "
        "WHERE txn_key = ? AND is_winner = 0 ORDER BY bid_amount DESC LIMIT 1",
        (txn_key,)
    ).fetchone()
    return row if row else (None, None)


def score(label, rows):
    """Mean absolute percentage error and hit rate of the predicted range."""
    if not rows:
        return
    errs = [abs(r['pred'] - r['actual']) / r['actual'] for r in rows]
    in_range = [r for r in rows if r.get('low') is not None
                and r['low'] <= r['actual'] <= r['high']]
    print(f"  {label}: MAPE {sum(errs) / len(errs) * 100:.1f}%"
          + (f", actual inside predicted range {len(in_range)}/{len(rows)}"
             if rows[0].get('low') is not None else ""))


def backtest(conn, txns, latest_date):
    bucket_avg_bids = dict(pd.read_sql(
        "SELECT bucket, avg_bids FROM bid_history_buckets "
        "WHERE scraped_at = (SELECT MAX(scraped_at) FROM bid_history_buckets)",
        conn
    ).itertuples(index=False, name=None))

    my_team_id = conn.execute(
        "SELECT team_id FROM team_balance WHERE is_me = 1 "
        "ORDER BY scraped_at DESC LIMIT 1"
    ).fetchone()
    my_team_id = my_team_id[0] if my_team_id else None

    # (1) What the model would genuinely have said before these auctions
    # resolved: the competitor model (rival lineups, squad depth, spending
    # capacity) is available, but the euro calibration is not — every
    # captured transaction was first seen on the very day board capture
    # started, so no earlier auction data exists to learn a premium from.
    # The calibration therefore falls back to the inherited 2%
    # assumption, which is exactly the position the model is in for the
    # NEXT auction it has to predict.
    prior_model, _, _ = recommenders.build_bid_competition_model(
        conn, latest_date, my_team_id=my_team_id
    )
    prior_model.markup_per_bidder = recommenders.FALLBACK_MARKUP_PER_BIDDER
    prior_model.calibration_samples = 0
    # (2) Calibrated on everything, including the rows being scored.
    tuned_model, _, _ = recommenders.build_bid_competition_model(
        conn, latest_date, my_team_id=my_team_id
    )

    print(f"\n{'=' * 78}\n1. BACKTEST — {len(txns)} real auction(s) with full bid data\n{'=' * 78}")
    if not len(txns):
        print("No captured transactions yet. Run the scraper and migration first.")
        return
    if len(txns) < recommenders.MIN_CALIBRATION_SAMPLES:
        print(f"⚠️  {len(txns)} transaction(s) is an anecdote, not a sample. Every number\n"
              f"    below is illustrative only — no error rate here is meaningful until\n"
              f"    the board feed has accumulated over many more days.")
    print("⚠️  Asking price is reconstructed by subtracting the player's own daily price\n"
          "    increment, and Biwenger bumps a player's price ON the sale — so for a\n"
          "    just-sold player that increment is partly caused by the very auction being\n"
          "    scored. Both the reconstructed asking price and any momentum read off it\n"
          "    are contaminated in a way a live market listing's are not. Treat the\n"
          "    direction of these results, not their precision.\n")

    old_rows, prior_rows, tuned_rows = [], [], []
    for t in txns.itertuples():
        rival_name, rival_amount = runner_up(conn, t.txn_key)
        old = old_model_bid(t.asking, t.price_change, bucket_avg_bids)
        prior = prior_model.suggest(t.asking, t.price_change, t.player_position)
        tuned = tuned_model.suggest(t.asking, t.price_change, t.player_position)

        print(f"\n  {t.player_name} ({t.player_position}) — captured {t.captured_on}")
        print(f"    asking ≈ {money(t.asking)} (price {money(t.player_price)} "
              f"− that day's {money(t.price_change)} increment)")
        print(f"    ACTUAL: sold for {money(t.winning_amount)} "
              f"({pct(t.winning_amount / t.asking - 1)} over asking) to {t.num_bidders} bidder(s); "
              f"runner-up {rival_name} at {money(rival_amount)}")
        print(f"    old (flat bucket markup):   {money(old)}  "
              f"[{pct(old / t.winning_amount - 1)} vs actual]")
        print(f"    new (prior knowledge only): {money(prior['suggested_bid_low'])} – "
              f"{money(prior['suggested_bid_high'])}, mid {money(prior['suggested_bid'])}  "
              f"[{pct(prior['suggested_bid'] / t.winning_amount - 1)} vs actual]")
        print(f"        expected {prior['expected_bidders']} bidders (actual {t.num_bidders}), "
              f"pressure {prior['competitor_pressure']}, "
              f"likely competitors: {prior['top_competitors']}")
        print(f"    new (calibrated in-sample): {money(tuned['suggested_bid_low'])} – "
              f"{money(tuned['suggested_bid_high'])}, mid {money(tuned['suggested_bid'])}  "
              f"[{pct(tuned['suggested_bid'] / t.winning_amount - 1)} vs actual]")

        old_rows.append({'pred': old, 'actual': t.winning_amount})
        prior_rows.append({'pred': prior['suggested_bid'], 'actual': t.winning_amount,
                           'low': prior['suggested_bid_low'], 'high': prior['suggested_bid_high']})
        tuned_rows.append({'pred': tuned['suggested_bid'], 'actual': t.winning_amount,
                           'low': tuned['suggested_bid_low'], 'high': tuned['suggested_bid_high']})

    print("\n  --- summary ---")
    score("old (flat bucket markup)  ", old_rows)
    score("new (prior knowledge only)", prior_rows)
    score("new (in-sample, NOT a validation — shows where calibration heads)", tuned_rows)


def sanity_checks(conn, date):
    """The two shapes that motivated the rework, plus a competitor read
    on every position, against today's real market."""
    my_team_id = conn.execute(
        "SELECT team_id FROM team_balance WHERE is_me = 1 AND scraped_at LIKE ?",
        (f"{date}%",)
    ).fetchone()
    my_team_id = my_team_id[0] if my_team_id else None
    model, bucket_avg_bids, _ = recommenders.build_bid_competition_model(
        conn, date, my_team_id=my_team_id
    )

    d = recommenders.resolve_scraped_at(conn, date)
    market = pd.read_sql(
        "SELECT position, club, name, price, change FROM market "
        "WHERE scraped_at LIKE ? ORDER BY price DESC",
        conn, params=(d,)
    )

    print(f"\n{'=' * 78}\n2. TODAY'S REAL MARKET — old suggestion vs new predicted range\n{'=' * 78}")
    print(f"  calibration: {model.calibration_samples} real auction(s), "
          f"{model.markup_per_bidder * 100:.2f}% per bidder "
          f"(fallback is {recommenders.FALLBACK_MARKUP_PER_BIDDER * 100:.2f}%)")
    print(f"  rivals modelled: {len(model.rivals)}\n")
    print(f"  {'player':<18}{'pos':<20}{'price':>12}{'change':>10}"
          f"{'old bid':>12}{'new low':>12}{'new high':>12}{'exp.bids':>10}  competitors")
    for r in market.itertuples():
        s = model.suggest(r.price, r.change, r.position)
        old = old_model_bid(r.price, r.change, bucket_avg_bids)
        print(f"  {r.name[:17]:<18}{str(r.position)[:19]:<20}{money(r.price):>12}"
              f"{money(r.change):>10}{money(old):>12}{money(s['suggested_bid_low']):>12}"
              f"{money(s['suggested_bid_high']):>12}{s['expected_bidders']:>10}"
              f"  {s['competitor_count']} ({s['top_competitors']})")

    print(f"\n{'=' * 78}\n3. THE CASES THIS WAS BUILT TO GET RIGHT\n{'=' * 78}")
    # Picked out of real scraped players rather than hardcoded by name,
    # so this keeps testing the right SHAPES as the season moves on:
    #   - an expensive player whose price is sliding, which must not be
    #     handed a big competitive markup just for being expensive
    #   - a cheap player climbing fast, which must get credit for that
    #     momentum even though it makes him look expensive per euro
    #   - a mid-priced flat player, as the unremarkable control
    squad = pd.read_sql(
        "SELECT name, position, price, change FROM team_players WHERE scraped_at LIKE ?",
        conn, params=(d,)
    )
    squad = squad[squad['price'] > 0].copy()
    squad.loc[:, 'move_pct'] = squad['change'].fillna(0) / squad['price']
    falling = squad[squad['change'] < 0].sort_values('price', ascending=False)
    rising = squad[(squad['change'] > 0) & (squad['price'] < 3_000_000)].sort_values('move_pct', ascending=False)
    flat = squad[squad['change'] == 0].sort_values('price', ascending=False)

    scenarios = []
    if len(falling):
        scenarios.append(("expensive and falling — must NOT get a big markup", falling.iloc[0]))
    if len(rising):
        scenarios.append(("cheap and rising fast — momentum should count", rising.iloc[0]))
    if len(flat):
        scenarios.append(("mid-price and flat — the unremarkable control", flat.iloc[0]))

    for label, c in scenarios:
        s = model.suggest(c['price'], c['change'], c['position'])
        old = old_model_bid(c['price'], c['change'], bucket_avg_bids)
        print(f"\n  {c['name']} — {label}")
        print(f"    {c['position']}, {money(c['price'])}, change {money(c['change'])} "
              f"({c['move_pct'] * 100:+.2f}% today)")
        print(f"    old suggestion: {money(old)} ({pct(old / c['price'] - 1)} over market)")
        print(f"    new range:      {money(s['suggested_bid_low'])} – {money(s['suggested_bid_high'])} "
              f"({pct(s['suggested_bid_low'] / c['price'] - 1)} – "
              f"{pct(s['suggested_bid_high'] / c['price'] - 1)} over market)")
        print(f"    {s['expected_bidders']} expected bidders, pressure {s['competitor_pressure']}, "
              f"{s['competitor_count']} plausible competitors: {s['top_competitors']}")

    print(f"\n{'=' * 78}\n4. POSITIONAL DEPTH THE COMPETITOR MODEL IS READING\n{'=' * 78}")
    print(f"  {'team':<26}{'capacity':>14}   depth (squad ÷ what their formation fields)")
    for rv in sorted(model.rivals, key=lambda x: -x['capacity']):
        depth = "  ".join(f"{k} {v:.2f}" for k, v in sorted(rv['depth'].items()))
        print(f"  {rv['team_name'][:25]:<26}{money(rv['capacity']):>14}   {depth}")


def main():
    conn = sqlite3.connect(DB)
    latest = (conn.execute("SELECT MAX(scraped_at) FROM market").fetchone()[0] or "")[:10]
    date = sys.argv[1] if len(sys.argv) > 1 else latest
    txns = load_transactions(conn)
    backtest(conn, txns, date)
    sanity_checks(conn, date)
    conn.close()


if __name__ == '__main__':
    main()
