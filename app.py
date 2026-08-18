from flask import Flask, render_template, request, jsonify
import sqlite3
import pandas as pd
import os
import unicodedata
from datetime import datetime

app = Flask(__name__)

# ---------- Player <-> start-probability matching ----------
# Biwenger (player/club names) and futbolfantasy.com (probability source)
# don't always agree on how to write the same real person: accents differ
# ("Atlético" vs "Atletico"), and some show a full name where the other
# shows a bare surname ("Antonio Rudiger" vs "Rudiger"). Exact match after
# accent-folding handles the first; a club-scoped surname/subset fallback
# (accepted only when it resolves to exactly one candidate, never a guess
# among several) handles the second.

def _strip_accents(s):
    if not s:
        return ''
    return ''.join(c for c in unicodedata.normalize('NFKD', str(s)) if not unicodedata.combining(c))

def _normalize_name(s):
    return _strip_accents(s).lower().strip()

def _build_probability_lookup(prob_df):
    """{normalized_club: [(normalized_name, probability), ...]}"""
    lookup = {}
    for _, row in prob_df.iterrows():
        club = _normalize_name(row['team_name'])
        lookup.setdefault(club, []).append((_normalize_name(row['player_name']), row['probability']))
    return lookup

def _find_probability(name, club, lookup):
    candidates = lookup.get(_normalize_name(club))
    if not candidates:
        return None
    target = _normalize_name(name)
    target_tokens = target.split()
    if not target_tokens:
        return None

    for cand_name, prob in candidates:
        if cand_name == target:
            return prob

    # Surname match: most reliable single signal for "Rudiger" vs
    # "Antonio Rudiger" — only trust it if exactly one squad-mate shares
    # that surname.
    target_last = target_tokens[-1]
    surname_matches = [p for cn, p in candidates if cn.split() and cn.split()[-1] == target_last]
    if len(surname_matches) == 1:
        return surname_matches[0]

    # Fallback: one name's full token set contained in the other's
    # ("El Hilali" ⊂ "Omar El Hilali"). Same one-candidate-only guard.
    target_set = set(target_tokens)
    subset_matches = []
    for cand_name, prob in candidates:
        cand_set = set(cand_name.split())
        shorter, longer = (target_set, cand_set) if len(target_set) <= len(cand_set) else (cand_set, target_set)
        if shorter and shorter.issubset(longer):
            subset_matches.append(prob)
    if len(subset_matches) == 1:
        return subset_matches[0]

    return None

def _attach_probabilities(df, prob_df, name_col='name', club_col='club'):
    lookup = _build_probability_lookup(prob_df)
    df = df.copy()
    df['probability'] = df.apply(
        lambda r: _find_probability(r[name_col], r[club_col], lookup) or '0%', axis=1
    )
    return df

def _accent_fold_sql(col):
    return (
        f"LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({col},'á','a'),'é','e'),'í','i'),'ó','o'),'ñ','n'))"
    )

# ---------- Buy/sell recommenders ----------
# Heuristic scoring over data we actually have — starting-XI probability,
# season/recent points, and (for buy) historical bid-count patterns by
# price bracket. Not a market-value model, just a way to surface and rank
# candidates worth a closer look; every input is shown alongside the score
# so the number is checkable, not a black box.

# Mirrors money_left.py's BID_BUCKETS — duplicated rather than imported
# (importing across the scraper/app boundary isn't worth it for six
# tuples; migration.py already does its own importlib dance for the
# heavier functions in that module).
BID_BUCKETS = [
    (0, 500_000, "<500k"),
    (500_000, 1_000_000, "500k-1M"),
    (1_000_000, 3_000_000, "1M-3M"),
    (3_000_000, 5_000_000, "3M-5M"),
    (5_000_000, 10_000_000, "5M-10M"),
    (10_000_000, float("inf"), "10M+"),
]

def _bucket_label(price):
    for lo, hi, label in BID_BUCKETS:
        if lo <= price < hi:
            return label
    return BID_BUCKETS[-1][2]

def _parse_pct(s):
    try:
        return float(str(s).rstrip('%'))
    except (ValueError, TypeError):
        return 0.0

def _normalize(series):
    """Min-max scale to 0-100. A constant or empty series has no signal to
    rank on, so it's given a neutral 50 rather than collapsing to 0 (which
    would look like the worst possible score, not "no data")."""
    s = series.astype(float)
    lo, hi = s.min(), s.max()
    if pd.isna(lo) or pd.isna(hi) or hi == lo:
        return pd.Series([50.0] * len(s), index=s.index)
    return (s - lo) / (hi - lo) * 100

def _json_safe(df):
    """NaN isn't valid JSON; swap it (and pandas NaT) for null before
    to_dict so the frontend gets a clean absent value instead of a NaN
    token some JSON parsers choke on."""
    return df.astype(object).where(pd.notna(df), None).to_dict('records')

def get_available_dates():
    conn = sqlite3.connect('data/biwenger_data.db')
    query = """
    SELECT DISTINCT DATE(scraped_at) as date 
    FROM (
        SELECT scraped_at FROM market
        UNION SELECT scraped_at FROM team_players
        UNION SELECT scraped_at FROM teams
    )
    ORDER BY date DESC
    """
    dates = pd.read_sql(query, conn)['date'].tolist()
    conn.close()
    return dates

@app.route('/')
def dashboard():
    dates = get_available_dates()
    selected_date = request.args.get('date', dates[0] if dates else None)

    return render_template(
        'dashboard.html',
        available_dates=dates,
        selected_date=selected_date,
    )

@app.route('/api/data')
def get_data():
    date = request.args.get('date')
    if not date:
        return jsonify({'error': 'Date parameter required'}), 400

    conn = sqlite3.connect('data/biwenger_data.db')
    conn.row_factory = sqlite3.Row

    # --- League standings ---
    standings = pd.read_sql(
        """
        SELECT position, name, points, value_change, is_me
        FROM teams
        WHERE scraped_at LIKE ?    -- match 'YYYY-MM-DD%'
        ORDER BY position
        """,
        conn, params=(f"{date}%",)
    )
    standings = standings.rename(columns={'position': 'pos', 'name': 'team'})

    # --- Market ---
    market = pd.read_sql(
        """
        SELECT position, club, name, price, change, status, recent_pts,
               this_season_pts, last_season_pts
        FROM market
        WHERE scraped_at LIKE ?
        ORDER BY price DESC
        LIMIT 50
        """,
        conn, params=(f"{date}%",)
    )

    # --- Team valuations summary ---
    # Group by team_id (stable across accent/casing variants) but display
    # the human-readable team name, not the normalized slug.
    teams_summary = pd.read_sql(
        """
        SELECT tp.team_id, MAX(tp.team) AS team_name, COUNT(*) AS player_count,
               SUM(tp.price) AS total_value,
               MAX(tb.ledger_balance) AS balance,
               MAX(tb.is_me) AS is_me,
               MAX(t.value_change) AS value_change
        FROM team_players tp
        LEFT JOIN team_balance tb
          ON tb.team_id = tp.team_id AND tb.scraped_at LIKE ?
        LEFT JOIN teams t
          ON t.name = tp.team AND t.scraped_at LIKE ?
        WHERE tp.scraped_at LIKE ?
        GROUP BY tp.team_id
        ORDER BY total_value DESC
        """,
        conn, params=(f"{date}%", f"{date}%", f"{date}%")
    )

    # Position breakdown per team (GK/DEF/MID/FWD counts) — dual-position
    # players ("Defender/Midfielder") count toward each position they cover.
    positions_df = pd.read_sql(
        "SELECT team_id, position FROM team_players WHERE scraped_at LIKE ?",
        conn, params=(f"{date}%",)
    )
    POSITION_LABELS = {'Goalkeeper': 'GK', 'Defender': 'DEF', 'Midfielder': 'MID', 'Forward': 'FWD'}
    pos_counts = {}
    for _, row in positions_df.iterrows():
        counts = pos_counts.setdefault(row['team_id'], {'GK': 0, 'DEF': 0, 'MID': 0, 'FWD': 0})
        for token in str(row['position']).split('/'):
            label = POSITION_LABELS.get(token.strip())
            if label:
                counts[label] += 1

    teams_summary = teams_summary.copy()
    teams_summary.loc[:, 'positions'] = teams_summary['team_id'].map(pos_counts)
    # Biwenger's own max-bid formula, confirmed against the live bid modal:
    # balance + 25% of total squad value (checked to the euro:
    # €368,300 + 0.25 x €68,050,000 = €17,380,800, matched exactly).
    teams_summary.loc[:, 'max_bid'] = teams_summary['balance'] + 0.25 * teams_summary['total_value']
    teams_summary = teams_summary.rename(
        columns={'team_name': 'team', 'player_count': 'players'}
    )

    # --- Team players for every team (the dashboard expands rosters inline
    # under each team's row rather than filtering to one team at a time) ---
    team_players = pd.read_sql(
        """
        SELECT team_id, position, club, name, price, change, this_season_pts,
               points_per_match, status
        FROM team_players
        WHERE scraped_at LIKE ?
        ORDER BY team_id, price DESC
        """,
        conn, params=(f"{date}%",)
    )

    # --- Start-probability matching (see _find_probability for why this
    # is done in Python rather than a SQL join: exact match after accent
    # folding, falling back to a club-scoped surname/subset match that's
    # only trusted when it resolves to exactly one candidate) ---
    prob_df = pd.read_sql(
        "SELECT player_name, team_name, probability FROM player_probabilities "
        "WHERE scraped_at LIKE ? AND probability != '0%'",
        conn, params=(f"{date}%",)
    )
    market = _attach_probabilities(market, prob_df)
    team_players = _attach_probabilities(team_players, prob_df)

    # --- Buy recommender: rank market listings by starting-XI probability,
    # a points-per-euro value score, and recent form, with a squad-need
    # flag (position counts below the league median for my team) and a
    # suggested-bid range derived from historical bid counts in the same
    # price bracket. ---
    my_team_row = teams_summary[teams_summary['is_me'] == 1]
    my_team_id = my_team_row['team_id'].iloc[0] if len(my_team_row) else None
    my_counts = pos_counts.get(my_team_id, {'GK': 0, 'DEF': 0, 'MID': 0, 'FWD': 0})
    position_medians = {}
    if pos_counts:
        for label in ('GK', 'DEF', 'MID', 'FWD'):
            vals = sorted(c[label] for c in pos_counts.values())
            n = len(vals)
            position_medians[label] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    def _needed_positions(position_str):
        needed = [
            POSITION_LABELS[token.strip()]
            for token in str(position_str).split('/')
            if POSITION_LABELS.get(token.strip())
            and my_counts.get(POSITION_LABELS[token.strip()], 0) < position_medians.get(POSITION_LABELS[token.strip()], 0)
        ]
        return ', '.join(needed) or None

    bid_buckets = pd.read_sql(
        "SELECT bucket, avg_bids, count FROM bid_history_buckets WHERE scraped_at LIKE ?",
        conn, params=(f"{date}%",)
    )
    bucket_avg_bids = dict(zip(bid_buckets['bucket'], bid_buckets['avg_bids']))
    bucket_sample_size = dict(zip(bid_buckets['bucket'], bid_buckets['count']))

    def _suggested_bid(price):
        avg_bids = bucket_avg_bids.get(_bucket_label(price))
        # No historical signings in this bracket yet -> fall back to a
        # flat 5% cushion above asking price rather than no suggestion.
        markup = 1 + min(avg_bids, 10) * 0.02 if avg_bids else 1.05
        return round(price * markup / 10_000) * 10_000

    buy = market.copy()
    buy.loc[:, 'start_pct'] = buy['probability'].apply(_parse_pct)
    buy.loc[:, 'blended_pts'] = buy['this_season_pts'].fillna(0) + buy['last_season_pts'].fillna(0) * 0.5
    buy.loc[:, 'value_score'] = buy.apply(
        lambda r: (r['blended_pts'] / (r['price'] / 1_000_000)) if r['price'] else 0, axis=1
    )
    buy.loc[:, 'recent_score'] = buy['recent_pts'].fillna(0)
    buy.loc[:, 'score'] = (
        0.45 * _normalize(buy['start_pct']) +
        0.35 * _normalize(buy['value_score']) +
        0.20 * _normalize(buy['recent_score'])
    ).round(1)
    buy.loc[:, 'squad_need'] = buy['position'].apply(_needed_positions)
    buy.loc[:, 'bid_bucket'] = buy['price'].apply(_bucket_label)
    buy.loc[:, 'bucket_avg_bids'] = buy['bid_bucket'].map(bucket_avg_bids)
    buy.loc[:, 'bucket_sample'] = buy['bid_bucket'].map(bucket_sample_size)
    buy.loc[:, 'suggested_bid'] = buy['price'].apply(_suggested_bid)
    # A relative 0-100 score only ever measures "best of today's market" —
    # even a mediocre snapshot has a top scorer. That's fine for ranking,
    # but not for deciding whether a suggested bid should exist at all:
    # a real chance of actually starting (>=40%) is a hard requirement,
    # and the score itself must clear a real quality bar, not just be the
    # least-bad option on a thin day.
    buy_candidates = buy[buy['start_pct'] >= 40]
    buy_recommendations = buy_candidates[buy_candidates['score'] >= 45].sort_values('score', ascending=False).head(12)

    # --- Sell recommender: my full roster, ranked by a mix of bench risk
    # (low starting-XI probability) and profit already banked (where a
    # purchase price is known — original-squad players have none, so they
    # rank purely on bench risk instead of being excluded). A live
    # purchase offer from another manager (see scraper.get_my_offers) is
    # the single strongest signal available — someone is offering real
    # money right now — so it overrides the heuristic score outright. ---
    sell_pool = pd.read_sql(
        f"""
        SELECT tp.name AS player, tp.club, tp.position, tp.price AS current_price,
               tp.this_season_pts, tp.last_season_pts, tp.status, op.buy_price,
               po.price AS offer_price
        FROM team_players tp
        JOIN team_balance tb ON tb.team_id = tp.team_id AND tb.is_me = 1 AND tb.scraped_at LIKE ?
        LEFT JOIN open_positions op ON op.team_id = tp.team_id AND op.scraped_at LIKE ?
          AND {_accent_fold_sql('op.player')} = {_accent_fold_sql('tp.name')}
        LEFT JOIN player_offers po ON po.scraped_at LIKE ?
          AND {_accent_fold_sql('po.player_name')} = {_accent_fold_sql('tp.name')}
        WHERE tp.scraped_at LIKE ?
        """,
        conn, params=(f"{date}%", f"{date}%", f"{date}%", f"{date}%")
    )
    sell_pool = _attach_probabilities(sell_pool, prob_df, name_col='player', club_col='club')
    sell_pool.loc[:, 'start_pct'] = sell_pool['probability'].apply(_parse_pct)
    sell_pool.loc[:, 'bench_score'] = 100 - sell_pool['start_pct']
    sell_pool.loc[:, 'profit'] = sell_pool['current_price'] - sell_pool['buy_price']
    sell_pool.loc[:, 'profit_pct'] = (sell_pool['profit'] / sell_pool['buy_price']).where(sell_pool['buy_price'] > 0)
    sell_pool.loc[:, 'injured'] = sell_pool['status'].fillna('').str.startswith(('Injured', 'Doubtful'))
    sell_pool.loc[:, 'has_offer'] = sell_pool['offer_price'].notna()
    sell_pool.loc[:, 'score'] = (
        0.5 * _normalize(sell_pool['bench_score']) +
        0.4 * _normalize(sell_pool['profit_pct'].fillna(0).clip(lower=0)) +
        0.1 * sell_pool['injured'].map({True: 100.0, False: 0.0})
    ).round(1)
    sell_pool.loc[sell_pool['has_offer'], 'score'] = 100.0

    # Only surface sells that actually make sense: a live cash offer
    # (always worth a look), meaningful banked profit on a player who
    # isn't nailed to the starting XI, a clear loss worth cutting before
    # it drops further, or bench fodder with no games and no cost basis
    # to protect. A nailed-on starter (>=70% start odds) only qualifies
    # if the payday is large enough to outweigh the points they'd score.
    profit_pct = sell_pool['profit_pct']
    is_starter = sell_pool['start_pct'] >= 70
    worth_selling = (
        sell_pool['has_offer']
        | (profit_pct.notna() & (profit_pct >= 0.15) & ~is_starter)
        | (profit_pct.notna() & (profit_pct <= -0.15))
        | (sell_pool['buy_price'].isna() & (sell_pool['start_pct'] <= 20))
        | (profit_pct.notna() & (profit_pct >= 0.5))
    )
    sell_recommendations = sell_pool[worth_selling].sort_values('score', ascending=False)

    # --- My trades: current holdings (unrealized profit) and completed
    # sales (realized profit), for the "My Trades" tab. Only meaningful
    # for the logged-in user's own team (is_me) — that's the only account
    # whose purchase history the forum ledger can be trusted to be complete
    # for from the moment we started scraping.
    my_holdings = pd.read_sql(
        f"""
        SELECT op.player, op.buy_price, op.count, tp.price AS current_price,
               tp.price - op.buy_price AS profit, tp.club, tp.position
        FROM open_positions op
        JOIN team_balance tb ON tb.team_id = op.team_id AND tb.is_me = 1 AND tb.scraped_at LIKE ?
        JOIN team_players tp ON tp.team_id = op.team_id AND tp.scraped_at LIKE ?
          AND {_accent_fold_sql('tp.name')} = {_accent_fold_sql('op.player')}
        WHERE op.scraped_at LIKE ?
        ORDER BY profit DESC
        """,
        conn, params=(f"{date}%", f"{date}%", f"{date}%")
    )

    my_sales = pd.read_sql(
        """
        SELECT rt.player, rt.buy_price, rt.sell_price, rt.profit
        FROM realized_trades rt
        JOIN team_balance tb ON tb.team_id = rt.team_id AND tb.is_me = 1 AND tb.scraped_at LIKE ?
        WHERE rt.scraped_at LIKE ? AND rt.profit IS NOT NULL
        ORDER BY rt.profit DESC
        """,
        conn, params=(f"{date}%", f"{date}%")
    )

    conn.close()

    return jsonify({
        'standings': standings.to_dict('records'),
        'market': market.to_dict('records'),
        'teams': teams_summary.to_dict('records'),
        'team_players': team_players.to_dict('records'),
        'my_holdings': my_holdings.to_dict('records'),
        'my_sales': my_sales.to_dict('records'),
        'buy_recommendations': _json_safe(buy_recommendations),
        'sell_recommendations': _json_safe(sell_recommendations),
        'date': date
    })

if __name__ == '__main__':
    # 5000 is macOS AirPlay Receiver's default port — use 5001 instead so
    # this doesn't collide with it out of the box.
    app.run(debug=True, port=int(os.getenv('PORT', 5001)))