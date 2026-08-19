"""Buy/sell recommendation logic, shared by the Flask dashboard (app.py)
and the standalone Telegram digest (notify.py) so the two never drift —
each just calls build_recommendations(conn, date) against its own DB
connection and gets back the same two ranked DataFrames.
"""
import unicodedata

import pandas as pd

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

def attach_probabilities(df, prob_df, name_col='name', club_col='club'):
    lookup = _build_probability_lookup(prob_df)
    df = df.copy()
    df['probability'] = df.apply(
        lambda r: _find_probability(r[name_col], r[club_col], lookup) or '0%', axis=1
    )
    return df

def accent_fold_sql(col):
    return (
        f"LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({col},'á','a'),'é','e'),'í','i'),'ó','o'),'ñ','n'))"
    )

def to_records(df):
    """NaN isn't valid JSON (and prints ugly in an f-string); swap it
    (and pandas NaT) for None before to_dict."""
    return df.astype(object).where(pd.notna(df), None).to_dict('records')

def resolve_scraped_at(conn, date):
    """A calendar date can have more than one scrape behind it — a manual
    workflow_dispatch rerun, a retry after a failure, testing — and every
    one of those runs shares the same 'YYYY-MM-DD' prefix. Querying with
    `scraped_at LIKE 'date%'` (the old approach, used everywhere) silently
    unions rows from ALL of that date's runs instead of picking one,
    which is exactly what looked like "accumulating instead of
    refreshing": double-counted market listings, duplicated squad rows,
    inflated recommendation pools.

    This resolves `date` to the single latest run's exact scraped_at
    timestamp, so callers can filter with `scraped_at = ?` and always get
    one consistent snapshot. Falls back to the `date%` prefix itself if
    nothing matches, so a caller that doesn't get a real hit here still
    behaves the same (correctly empty) way it did before.
    """
    row = conn.execute(
        "SELECT MAX(scraped_at) FROM teams WHERE scraped_at LIKE ?",
        (f"{date}%",)
    ).fetchone()
    return row[0] if row and row[0] else f"{date}%"

POSITION_LABELS = {'Goalkeeper': 'GK', 'Defender': 'DEF', 'Midfielder': 'MID', 'Forward': 'FWD'}

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

def build_recommendations(conn, date):
    """Returns (buy_recommendations, sell_recommendations) — both filtered
    to candidates that actually clear a real quality bar, not just "top N
    of whatever's available today" (see the buy/sell filtering comments
    below for what "clears the bar" means for each)."""
    # Exact match on one resolved run, not a `date%` prefix that could
    # span more than one run from the same calendar day — see
    # resolve_scraped_at's docstring.
    d = resolve_scraped_at(conn, date)

    market = pd.read_sql(
        """
        SELECT position, club, name, price, change, status, recent_pts,
               this_season_pts, last_season_pts
        FROM market
        WHERE scraped_at LIKE ?
        ORDER BY price DESC
        LIMIT 50
        """,
        conn, params=(d,)
    )

    teams_summary = pd.read_sql(
        """
        SELECT tp.team_id, MAX(tb.is_me) AS is_me
        FROM team_players tp
        LEFT JOIN team_balance tb ON tb.team_id = tp.team_id AND tb.scraped_at LIKE ?
        WHERE tp.scraped_at LIKE ?
        GROUP BY tp.team_id
        """,
        conn, params=(d, d)
    )

    # Position breakdown per team (GK/DEF/MID/FWD counts) — dual-position
    # players ("Defender/Midfielder") count toward each position they cover.
    positions_df = pd.read_sql(
        "SELECT team_id, position FROM team_players WHERE scraped_at LIKE ?",
        conn, params=(d,)
    )
    pos_counts = {}
    for _, row in positions_df.iterrows():
        counts = pos_counts.setdefault(row['team_id'], {'GK': 0, 'DEF': 0, 'MID': 0, 'FWD': 0})
        for token in str(row['position']).split('/'):
            label = POSITION_LABELS.get(token.strip())
            if label:
                counts[label] += 1

    prob_df = pd.read_sql(
        "SELECT player_name, team_name, probability FROM player_probabilities "
        "WHERE scraped_at LIKE ? AND probability != '0%'",
        conn, params=(d,)
    )
    market = attach_probabilities(market, prob_df)

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
        conn, params=(d,)
    )
    bucket_avg_bids = dict(zip(bid_buckets['bucket'], bid_buckets['avg_bids']))
    bucket_sample_size = dict(zip(bid_buckets['bucket'], bid_buckets['count']))

    def _suggested_bid(price, change):
        avg_bids = bucket_avg_bids.get(_bucket_label(price))
        # No historical signings in this bracket yet -> fall back to a
        # flat 5% cushion above asking price rather than no suggestion.
        bucket_markup = min(avg_bids, 10) * 0.02 if avg_bids else 0.05
        # Momentum: a player already rising fast is likely still rising
        # by the time a bid actually resolves, and fast risers typically
        # draw more competing interest in the first place — a plain
        # percentage-of-today's-price markup doesn't account for either.
        # Add today's own growth as a floor on top of the competition
        # markup (only when rising — a flat/falling price adds nothing).
        momentum = max(change, 0) if pd.notna(change) else 0
        return round((price * (1 + bucket_markup) + momentum) / 10_000) * 10_000

    buy = market.copy()
    buy.loc[:, 'start_pct'] = buy['probability'].apply(_parse_pct)
    buy.loc[:, 'blended_pts'] = buy['this_season_pts'].fillna(0) + buy['last_season_pts'].fillna(0) * 0.5
    # Two different lenses on blended_pts: talent_score is "how good is
    # this player, full stop" (rewards proven output regardless of
    # price); value_score is "how good per euro" (rewards bargains).
    # Scoring on value_score alone systematically buried expensive,
    # clearly-good players — a €9M player with a strong points history
    # scores terribly on points-per-euro next to a €200k bench option,
    # even though "clearly a good player" is exactly the kind of signal
    # a buy recommender should surface.
    buy.loc[:, 'talent_score'] = buy['blended_pts']
    buy.loc[:, 'value_score'] = buy.apply(
        lambda r: (r['blended_pts'] / (r['price'] / 1_000_000)) if r['price'] else 0, axis=1
    )
    buy.loc[:, 'recent_score'] = buy['recent_pts'].fillna(0)
    # Percentage move, not raw euros — a €90 drop matters differently on
    # a €150k player than an €12M one. Rewards a price the market is
    # actively bidding up (real demand) and penalizes one sliding down
    # (a real risk signal — declining form, a doubt, reduced role — not
    # just noise to ignore, even though it's only one day's reading).
    buy.loc[:, 'momentum_pct'] = buy.apply(
        lambda r: (r['change'] / r['price'] * 100) if r['price'] else 0, axis=1
    )
    buy.loc[:, 'score'] = (
        0.30 * _normalize(buy['start_pct']) +
        0.25 * _normalize(buy['talent_score']) +
        0.20 * _normalize(buy['value_score']) +
        0.10 * _normalize(buy['recent_score']) +
        0.15 * _normalize(buy['momentum_pct'])
    ).round(1)
    buy.loc[:, 'squad_need'] = buy['position'].apply(_needed_positions)
    buy.loc[:, 'bid_bucket'] = buy['price'].apply(_bucket_label)
    buy.loc[:, 'bucket_avg_bids'] = buy['bid_bucket'].map(bucket_avg_bids)
    buy.loc[:, 'bucket_sample'] = buy['bid_bucket'].map(bucket_sample_size)
    buy.loc[:, 'suggested_bid'] = buy.apply(lambda r: _suggested_bid(r['price'], r['change']), axis=1)
    # A relative 0-100 score only ever measures "best of today's market" —
    # even a mediocre snapshot has a top scorer. That's fine for ranking,
    # but not for deciding whether a suggested bid should exist at all:
    # a real chance of actually starting (>=40%) is a hard requirement,
    # and the score itself must clear a real quality bar, not just be the
    # least-bad option on a thin day.
    buy_candidates = buy[buy['start_pct'] >= 40]
    buy_recommendations = buy_candidates[buy_candidates['score'] >= 40].sort_values('score', ascending=False).head(12)

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
          AND {accent_fold_sql('op.player')} = {accent_fold_sql('tp.name')}
        LEFT JOIN player_offers po ON po.scraped_at LIKE ?
          AND {accent_fold_sql('po.player_name')} = {accent_fold_sql('tp.name')}
        WHERE tp.scraped_at LIKE ?
        """,
        conn, params=(d, d, d, d)
    )
    sell_pool = attach_probabilities(sell_pool, prob_df, name_col='player', club_col='club')
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

    # --- Affordability: Biwenger requires a non-negative balance heading
    # into each round, so "can I bid this" isn't just "is it under my
    # max-bid limit" (balance + 25% of squad value — a credit line, not
    # cash on hand). A bid can be within that limit and still force
    # panic-selling before the next round if it's not actually covered
    # by cash plus players that were worth selling anyway. shortfall is
    # what's left to cover after today's balance; raisable_from_sells is
    # what today's own sell recommendations would free up if all sold —
    # if that doesn't cover the shortfall, funding this buy would mean
    # selling players that don't otherwise make sense to let go of. ---
    # Prefer the live-scraped ground truth over the forum-post-ledger
    # reconstruction when we have it — the ledger is an approximation
    # (FIFO-matched from parsed forum posts) and can drift from Biwenger's
    # own number; actual_balance is only ever populated for our own team,
    # which is the only one this check needs anyway.
    balance_row = pd.read_sql(
        "SELECT COALESCE(actual_balance, ledger_balance) AS balance "
        "FROM team_balance WHERE is_me = 1 AND scraped_at LIKE ?",
        conn, params=(d,)
    )
    my_balance = float(balance_row['balance'].iloc[0]) if len(balance_row) else 0.0
    sorted_sells = sell_recommendations.sort_values('score', ascending=False)
    sell_names = sorted_sells['player'].tolist()
    sell_prices = sorted_sells['current_price'].tolist()
    raisable_from_sells = float(sum(sell_prices))

    def _funding_plan(shortfall):
        """Greedy: your best sell-candidates first (the ones you'd want
        to move anyway), stopping as soon as they cover the shortfall —
        names the actual players, not just a euro total, so "is this
        worth it" is a real judgment call instead of a hidden number."""
        if shortfall <= 0:
            return ''
        total, names = 0.0, []
        for name, price in zip(sell_names, sell_prices):
            names.append(name)
            total += price
            if total >= shortfall:
                break
        return ', '.join(names)

    buy_recommendations = buy_recommendations.copy()
    buy_recommendations.loc[:, 'shortfall'] = (buy_recommendations['suggested_bid'] - my_balance).clip(lower=0)
    buy_recommendations.loc[:, 'raisable_from_sells'] = raisable_from_sells
    buy_recommendations.loc[:, 'funded_without_hard_choices'] = buy_recommendations['shortfall'] <= raisable_from_sells
    buy_recommendations.loc[:, 'funding_plan'] = buy_recommendations['shortfall'].apply(_funding_plan)

    return buy_recommendations, sell_recommendations
