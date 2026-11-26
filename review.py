"""After-matchweek review: what went wrong in a finished round, and what to
do differently. Run from notify.py once a round has finished, or by hand:

    python review.py            # review every finished round not yet reviewed
    python review.py 4911       # (re)print one round, without saving

Each review compares my round with the league and with the best I could
have done with the players I actually had:
  - points, rank in the round, gap to the round's best team;
  - bench mistakes: the best valid XI from my own squad vs. what I fielded;
  - captain: my captain's points vs. the best starter under the €5M cap;
  - starters who didn't play at all;
  - a zeroed round (negative balance at the lock);
  - bids I lost by under 5% before the round, and what those players scored.

Lessons are rule-based and plain-language. They are appended to
csvs/others/round_reviews.csv (one row per round, append-only, never
rewritten) and lessons.md, so they survive in git and can be read back by
later runs: notify.py shows the latest review plus any lesson that keeps
coming back.
"""
import json
import os
import sqlite3
import sys

import pandas as pd

import recommenders

DB = 'data/biwenger_data.db'
REVIEWS_PATH = 'csvs/others/round_reviews.csv'
LESSONS_PATH = 'lessons.md'
REVIEW_COLUMNS = ['round_id', 'round_name', 'my_points', 'round_rank', 'teams',
                  'best_team', 'best_points', 'league_avg', 'bench_lost',
                  'captain_lost', 'dnp', 'zeroed', 'close_bids_lost', 'lessons']
FORMATIONS = recommenders.BEST_XI_FORMATIONS
POS = {1: 'GK', 2: 'DEF', 3: 'MID', 4: 'FWD'}
CLOSE_BID_PCT = 0.05
CAPTAIN_CAP = recommenders.STARTING_XI_PRICE_CAP

# A lesson is reported as "keeps happening" when its kind shows up in at
# least this many of the last RECENT_ROUNDS reviews.
RECENT_ROUNDS = 5
RECURRING_MIN = 2


def _my_team_name(conn):
    row = conn.execute("SELECT team_name FROM team_balance WHERE is_me = 1 "
                       "ORDER BY scraped_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


def finished_rounds(conn):
    """Round ids whose fixtures have all finished, oldest first."""
    df = pd.read_sql("SELECT round_id, round_name, game_status FROM round_scores", conn)
    if not len(df):
        return []
    # Postponed fixtures are played later under a separate round, so a
    # round whose other games are all finished counts as done.
    done = df.groupby(['round_id', 'round_name'])['game_status'].apply(
        lambda s: s.isin(['finished', 'postponed']).all() and (s == 'finished').any()).reset_index()
    return [(int(r.round_id), r.round_name) for r in done[done['game_status']].itertuples()]


def _captains(conn, round_id):
    """{team_name: (captain player_id, price)} from the last lineup
    snapshot of that round, i.e. the lineup that locked."""
    rl = pd.read_sql("SELECT team_name, player_id, price, is_captain, scraped_at FROM rival_lineups "
                     "WHERE round_id = ?", conn, params=(round_id,))
    if not len(rl):
        return {}, pd.DataFrame()
    last = rl[rl['scraped_at'] == rl.groupby('team_name')['scraped_at'].transform('max')]
    caps = last[last['is_captain'] == 1]
    return ({r.team_name: (r.player_id, r.price) for r in caps.itertuples()},
            last[['team_name', 'player_id', 'price']])


def _best_xi_points(squad):
    """Best total over the valid formations, from (position, points) rows."""
    by = {k: sorted(squad[squad['pos'] == k]['pts'], reverse=True) for k in ('GK', 'DEF', 'MID', 'FWD')}
    best = None
    for d, m, f in FORMATIONS:
        if len(by['GK']) < 1 or len(by['DEF']) < d or len(by['MID']) < m or len(by['FWD']) < f:
            continue
        total = by['GK'][0] + sum(by['DEF'][:d]) + sum(by['MID'][:m]) + sum(by['FWD'][:f])
        best = total if best is None else max(best, total)
    return best


LOCKS_PATH = 'csvs/others/round_locks.csv'


def _round_locks():
    """{round_id: lock unix time}, logged daily from next_round.json by
    notify.py. Empty when no lock has been logged yet."""
    if not os.path.exists(LOCKS_PATH) or os.path.getsize(LOCKS_PATH) == 0:
        return {}
    locks = pd.read_csv(LOCKS_PATH)
    return {int(r.round_id): int(r.start) for r in locks.itertuples()}


def _lock_balance(conn, round_id, locks):
    """My balance from the last snapshot taken before the round's lock
    (scraped_at is stored in UTC), when the lock time is known."""
    if round_id not in locks:
        return None
    lock_utc = pd.Timestamp(locks[round_id], unit='s').strftime('%Y-%m-%d %H:%M:%S')
    bal = conn.execute(
        "SELECT COALESCE(actual_balance, ledger_balance) FROM team_balance "
        "WHERE is_me = 1 AND scraped_at <= ? ORDER BY scraped_at DESC LIMIT 1",
        (lock_utc,)).fetchone()
    return bal[0] if bal else None


def _confirmed_zero(conn, round_id, locks):
    """True when the standings show my season total didn't move over this
    round: the last snapshot before its lock vs the last one before the
    next lock (or the latest). The balance snapshot alone can't prove a
    zero, since it is taken in the morning and I may sell before an
    evening lock. None when there aren't snapshots on both sides."""
    if round_id not in locks:
        return None
    lock = locks[round_id]
    later = [t for t in locks.values() if t > lock]
    utc = lambda t: pd.Timestamp(t, unit='s').strftime('%Y-%m-%d %H:%M:%S')
    q = ("SELECT points FROM teams WHERE is_me = 1 AND scraped_at {} ? "
         "ORDER BY scraped_at DESC LIMIT 1")
    before = conn.execute(q.format('<='), (utc(lock),)).fetchone()
    after = conn.execute(q.format('<='), (utc(min(later)) if later else '9999',)).fetchone()
    if not before or not after:
        return None
    return after[0] == before[0]


def review_round(conn, round_id, round_name=None, me=None):
    me = me or _my_team_name(conn)
    rs = pd.read_sql("SELECT round_name, player_id, player, position, team, lineup_slot, points, played, "
                     "game_status "
                     "FROM round_scores WHERE round_id = ?", conn, params=(round_id,))
    if not len(rs) or me not in set(rs['team']):
        return None
    round_name = round_name or rs['round_name'].iloc[0]
    rs.loc[:, 'pts'] = rs['points'].fillna(0)
    rs.loc[:, 'pos'] = rs['position'].map(POS)
    captains, lineup_prices = _captains(conn, round_id)

    # Every team's round score: starters + coach + captain bonus (the
    # captain's points count twice, which matches the standings).
    totals = {}
    for team, g in rs[rs['team'].notna() & (rs['team'] != '')].groupby('team'):
        base = g[g['lineup_slot'].isin(['starter', 'coach'])]['pts'].sum()
        cap = captains.get(team)
        bonus = g[g['player_id'] == cap[0]]['pts'].sum() if cap else 0
        totals[team] = float(base + bonus)
    # A negative balance at the lock scores the whole round as 0.
    locks = _round_locks()
    balance = _lock_balance(conn, round_id, locks)
    zeroed = balance is not None and balance < 0
    # Only overwrite the score when the standings confirm the zero.
    if zeroed and _confirmed_zero(conn, round_id, locks):
        totals[me] = 0.0
    mine = rs[rs['team'] == me]
    my_points = totals.get(me, 0.0)
    ranking = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    rank = [t for t, _ in ranking].index(me) + 1
    best_team, best_points = ranking[0]
    league_avg = sum(totals.values()) / len(totals)

    # Bench: best valid XI from the players I had for this round.
    squad = mine[mine['lineup_slot'].isin(['starter', 'reserve']) & mine['pos'].notna()]
    fielded = mine[mine['lineup_slot'] == 'starter']['pts'].sum()
    best_xi = _best_xi_points(squad)
    bench_lost = float(best_xi - fielded) if best_xi is not None and best_xi > fielded else 0.0
    swaps = []
    if bench_lost > 0:
        benched = squad[squad['lineup_slot'] == 'reserve'].sort_values('pts', ascending=False)
        for b in benched.itertuples():
            worse = mine[(mine['lineup_slot'] == 'starter') & (mine['pos'] == b.pos) & (mine['pts'] < b.pts)]
            if len(worse):
                w = worse.sort_values('pts').iloc[0]
                swaps.append(f"{b.player} ({b.pts:g}) over {w['player']} ({w['pts']:g})")

    # Captain: best starter under the cap, priced at the lineup snapshot.
    captain_lost, cap_note = 0.0, None
    cap = captains.get(me)
    if cap is not None and len(lineup_prices):
        prices = lineup_prices[lineup_prices['team_name'] == me].set_index('player_id')['price']
        starters = mine[mine['lineup_slot'] == 'starter'].copy()
        starters.loc[:, 'price'] = starters['player_id'].map(prices)
        eligible = starters[starters['price'] < CAPTAIN_CAP]
        cap_pts = float(starters[starters['player_id'] == cap[0]]['pts'].sum())
        if len(eligible):
            top = eligible.sort_values('pts', ascending=False).iloc[0]
            captain_lost = max(float(top['pts']) - cap_pts, 0.0)
            cap_name = starters[starters['player_id'] == cap[0]]['player']
            if captain_lost > 0:
                cap_note = (f"{top['player']} ({top['pts']:g}) instead of "
                            f"{cap_name.iloc[0] if len(cap_name) else 'your captain'} ({cap_pts:g})")

    # Only finished games: a postponed fixture isn't a starter who didn't play.
    dnp = mine[(mine['lineup_slot'] == 'starter') & (mine['played'] == 0)
               & (mine['game_status'] == 'finished')]['player'].tolist()

    # Bids lost by under 5% in the week before this round.
    close = []
    bids = pd.read_sql(
        "SELECT txn_date, player_id, player_name, bid_amount, winning_amount, winner_name "
        "FROM market_bid_history WHERE bidder_name = ? AND is_winner = 0", conn, params=(me,))
    # Only auctions between the previous round's lock and this one, so one
    # lost bid is reported in exactly one round. Without logged locks, fall
    # back to the 8 days before this round's last lineup snapshot.
    start = end = None
    if round_id in locks:
        end = locks[round_id]
        earlier = [t for rid, t in locks.items() if rid != round_id and t < end]
        start = max(earlier) if earlier else end - 8 * 86400
    else:
        window = conn.execute("SELECT MAX(scraped_at) FROM rival_lineups WHERE round_id = ?",
                              (round_id,)).fetchone()
        if window and window[0]:
            end = pd.Timestamp(window[0]).timestamp()
            start = end - 8 * 86400
    if end is None:
        bids = bids.iloc[0:0]
    if len(bids):
        bids = bids[(bids['txn_date'] > start) & (bids['txn_date'] <= end)]
        bids = bids[(bids['winning_amount'] - bids['bid_amount']) / bids['bid_amount'] < CLOSE_BID_PCT]
        for b in bids.itertuples():
            scored = rs[rs['player_id'] == b.player_id]['pts']
            if len(scored):
                close.append((b.player_name, float(b.winning_amount - b.bid_amount), float(scored.sum())))

    lessons = []
    if not captains:
        lessons.append(('note', "No lineup snapshot for this round, so captain bonuses are missing "
                                "and points and rank may be a little off."))
    if zeroed:
        if totals[me] == 0:
            lessons.append(('balance', "The round scored 0 because the balance was negative at the lock. "
                                       "Sell before the deadline, every time."))
        else:
            lessons.append(('balance', "The balance was still negative on the morning of the lock. "
                                       "Get above €0 a day early, so the round is never at risk."))
    if dnp:
        lessons.append(('dnp', f"{len(dnp)} starter(s) didn't play: {', '.join(dnp)}. Check start % the "
                               "day before the lock and keep a bench player ready for each position."))
    if bench_lost >= 4:
        lessons.append(('bench', f"Bench choices cost {bench_lost:g} pts: "
                                 + ("; ".join(swaps[:2]) if swaps else "a different formation scored more")
                                 + "."))
    if captain_lost >= 4:
        lessons.append(('captain', f"Captain {cap_note} would have added {captain_lost:g} pts."))
    for name, gap, pts in close:
        if pts >= 6:
            lessons.append(('bids', f"Lost {name} by only {recommenders_money(gap)}; he scored {pts:g} this round. "
                                    "For players you really want, bid the 'safe' amount."))
    if rank <= 3 and not [k for k, _ in lessons if k != 'note']:
        lessons.append(('good', f"Good round: {rank}º of {len(totals)}. Keep the same approach."))

    return {
        'round_id': round_id, 'round_name': round_name, 'my_points': my_points,
        'round_rank': rank, 'teams': len(totals), 'best_team': best_team,
        'best_points': best_points, 'league_avg': round(league_avg, 1),
        'bench_lost': bench_lost, 'captain_lost': captain_lost, 'dnp': len(dnp),
        'zeroed': bool(zeroed), 'close_bids_lost': len(close),
        'lessons': json.dumps(lessons, ensure_ascii=False),
    }


def recommenders_money(n):
    return f"€{n:,.0f}"


def load_reviews():
    if not os.path.exists(REVIEWS_PATH):
        return pd.DataFrame(columns=REVIEW_COLUMNS)
    # An empty file is treated as no reviews. Any other parse error raises:
    # falling back to an empty frame would let review_new_rounds
    # overwrite the whole history.
    if os.path.getsize(REVIEWS_PATH) == 0:
        return pd.DataFrame(columns=REVIEW_COLUMNS)
    return pd.read_csv(REVIEWS_PATH)


def review_new_rounds(conn):
    """Review every finished round that has no saved review yet, and save
    them (append-only: an existing round's row is never rewritten).
    Returns the new review rows, oldest first."""
    done = load_reviews()
    seen = set(done['round_id'].astype(int)) if len(done) else set()
    me = _my_team_name(conn)
    new = []
    for round_id, name in finished_rounds(conn):
        if round_id in seen:
            continue
        r = review_round(conn, round_id, name, me)
        if r:
            new.append(r)
    if new:
        out = pd.concat([done, pd.DataFrame(new)], ignore_index=True)[REVIEW_COLUMNS]
        out.to_csv(REVIEWS_PATH, index=False)
        _append_lessons_md(new)
    return new


def _append_lessons_md(rows):
    lines = []
    if not os.path.exists(LESSONS_PATH):
        lines.append("# Lessons from each matchweek\n\nWritten automatically after every finished "
                     "round by review.py. Newest at the bottom.\n")
    for r in rows:
        lessons = json.loads(r['lessons'])
        lines.append(f"\n## {r['round_name']}: {r['my_points']:g} pts, {r['round_rank']}º of {r['teams']} "
                     f"(best {r['best_team']} {r['best_points']:g}, average {r['league_avg']:g})\n")
        for _, text in lessons or [('', 'Nothing stood out.')]:
            lines.append(f"- {text}\n")
    with open(LESSONS_PATH, 'a') as f:
        f.writelines(lines)


def recurring_lessons(reviews=None):
    """Lesson kinds that keep coming back in the last RECENT_ROUNDS
    reviews, as {kind: count}. 'good' never counts as a problem."""
    reviews = load_reviews() if reviews is None else reviews
    if not len(reviews):
        return {}
    counts = {}
    for raw in reviews.sort_values('round_id').tail(RECENT_ROUNDS)['lessons']:
        for kind, _ in json.loads(raw):
            if kind not in ('good', 'note'):
                counts[kind] = counts.get(kind, 0) + 1
    return {k: v for k, v in counts.items() if v >= RECURRING_MIN}


RECURRING_ADVICE = {
    'balance': "Negative balance at the lock keeps happening. Treat €0 at the lock as a hard rule.",
    'dnp': "Starters not playing keeps happening. Add cheap bench players who start, and check start % the day before.",
    'bench': "Bench choices keep costing points. Follow the Best XI suggestion instead of a hunch.",
    'captain': "Captain choices keep costing points. Use the Best XI captain (best outfield player under €5M).",
    'bids': "Close bid losses keep happening. Bid the 'safe' amount for players you really want.",
}


def format_review(r):
    lessons = json.loads(r['lessons'])
    head = (f"📋 <b>{r['round_name']} review</b>: {r['my_points']:g} pts, {r['round_rank']}º of {r['teams']} "
            f"(best: {r['best_team']} {r['best_points']:g}, average {r['league_avg']:g})")
    body = [f"  • {text}" for _, text in lessons] or ["  • Nothing stood out."]
    return "\n".join([head] + body)


def main():
    conn = sqlite3.connect(DB)
    if len(sys.argv) > 1:
        r = review_round(conn, int(sys.argv[1]))
        print(format_review(r) if r else "No data for that round.")
        return
    for r in review_new_rounds(conn):
        print(format_review(r))
        print()
    rec = recurring_lessons()
    for kind in rec:
        print("🔁", RECURRING_ADVICE.get(kind, kind))


if __name__ == '__main__':
    main()
