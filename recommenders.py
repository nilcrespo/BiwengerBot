"""Buy/sell recommendation logic, shared by the Flask dashboard (app.py)
and the standalone Telegram digest (notify.py) so the two never drift —
each just calls build_recommendations(conn, date) against its own DB
connection and gets back the same two ranked DataFrames.
"""
import unicodedata
from itertools import combinations

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
    """SQL equivalent of _normalize_name's accent-stripping, for joins that
    have to happen in SQL rather than pandas. Must cover the same character
    set _normalize_name (NFKD-based, strips any accent) and scraper.py's
    normalize_player_name handle, plus their uppercase forms explicitly:
    SQLite's LOWER() only folds ASCII without the ICU extension, so an
    accented capital (e.g. the Á in "Álvaro") survives a trailing LOWER()
    untouched and needs its own REPLACE.
    """
    pairs = [
        ('á', 'a'), ('Á', 'a'), ('à', 'a'), ('À', 'a'), ('ã', 'a'), ('Ã', 'a'),
        ('é', 'e'), ('É', 'e'), ('è', 'e'), ('È', 'e'),
        ('í', 'i'), ('Í', 'i'), ('ï', 'i'), ('Ï', 'i'),
        ('ó', 'o'), ('Ó', 'o'), ('ò', 'o'), ('Ò', 'o'),
        ('ú', 'u'), ('Ú', 'u'), ('ü', 'u'), ('Ü', 'u'),
        ('ñ', 'n'), ('Ñ', 'n'),
        ('ç', 'c'), ('Ç', 'c'),
    ]
    expr = col
    for accented, plain in pairs:
        expr = f"REPLACE({expr},'{accented}','{plain}')"
    return f"LOWER({expr})"

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

# Rounds into the season at which this_season_pts is trusted as the
# whole picture and last_season_pts stops counting at all. La Liga runs
# 38 rounds; ~10 (roughly a quarter of the season) is the rough point
# real analytics starts treating current-season sample size as reliable
# on its own. Adjustable without touching the interpolation logic below.
BLEND_TRANSITION_ROUNDS = 10
# How much weight this season's points carry at round 0, before it's had
# any chance to accumulate a real sample (see _blended_pts's docstring
# for why this used to be 0.5 and why that was too high this early on).
THIS_WEIGHT_FLOOR = 0.15

def _blended_pts(df, rounds_played):
    """A talent/output estimate blending both seasons, with the mix
    sliding from "mostly last season" to "only this season" as the
    season itself progresses — not a fixed split. At round 0,
    last_season_pts carries full weight and this_season_pts a small
    floor (THIS_WEIGHT_FLOOR — one or two matches is genuinely too
    noisy to lean on much yet, even though a hot start is real signal
    worth SOME weight). By BLEND_TRANSITION_ROUNDS, last_season_pts has
    decayed to zero weight and this_season_pts carries full weight —
    enough matches have been played that current form should stand on
    its own, and a club promoted since last season (last_season_pts=0,
    not necessarily a worse player) stops being penalized for something
    that was never a real signal about them in the first place.

    THIS_WEIGHT_FLOOR was 0.5 (this season already got HALF weight on
    day one of the season) until it was pointed out live, early in a new
    season, that this let one or two rounds of noise compete on close to
    even footing with a full prior season's real sample — exactly
    backwards this early on. Doesn't rescue a player with truly zero
    recorded output in BOTH fields (a genuine outside-league transfer
    with no history this source tracks) — 0 times any weight is still 0,
    so a real "insufficient data" flag for that case is a separate,
    unbuilt piece of work, not something this weighting alone can fix.
    """
    progress = min(max(rounds_played, 0) / BLEND_TRANSITION_ROUNDS, 1.0)
    last_weight = 1.0 - progress
    this_weight = THIS_WEIGHT_FLOOR + (1.0 - THIS_WEIGHT_FLOOR) * progress
    return df['last_season_pts'].fillna(0) * last_weight + df['this_season_pts'].fillna(0) * this_weight

def build_recommendations(conn, date):
    """Returns (buy_recommendations, sell_recommendations) — both filtered
    to candidates that actually clear a real quality bar, not just "top N
    of whatever's available today" (see the buy/sell filtering comments
    below for what "clears the bar" means for each)."""
    # Exact match on one resolved run, not a `date%` prefix that could
    # span more than one run from the same calendar day — see
    # resolve_scraped_at's docstring.
    d = resolve_scraped_at(conn, date)

    # How far into the season are we? team_players.played (matches
    # actually appeared in) isn't itself a round counter, but the
    # highest value across the whole scraped roster set is a reliable
    # proxy for it — anyone who's played every match so far has played
    # == rounds elapsed. Feeds _blended_pts' last-season/this-season mix.
    rounds_row = pd.read_sql(
        "SELECT MAX(played) AS rounds FROM team_players WHERE scraped_at LIKE ?",
        conn, params=(d,)
    )
    rounds_played = int(rounds_row['rounds'].iloc[0]) if len(rounds_row) and pd.notna(rounds_row['rounds'].iloc[0]) else 0

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
        change = change if pd.notna(change) else 0
        if change > 0:
            # Rising: a fast riser is likely still rising by the time a
            # bid resolves, and tends to draw more competing interest in
            # the first place — add today's own growth on top of the
            # full competition markup, not just a plain % of today's price.
            return round((price * (1 + bucket_markup) + change) / 10_000) * 10_000
        # Flat or falling: the bucket markup is a bracket-wide average
        # of how much competition players in this price range typically
        # draw — but a falling price is itself evidence THIS player
        # isn't in demand right now, so that average doesn't really
        # apply to him. Bid only a small fraction of the usual cushion,
        # not the full competitive markup, on top of today's price.
        return round(price * (1 + bucket_markup * 0.25) / 10_000) * 10_000

    buy = market.copy()
    buy.loc[:, 'start_pct'] = buy['probability'].apply(_parse_pct)
    buy.loc[:, 'blended_pts'] = _blended_pts(buy, rounds_played)
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
    sell_pool.loc[:, 'has_offer'] = sell_pool['offer_price'].notna()
    # Profit should reflect what you'd actually be paid, not an abstract
    # valuation — when there's a live offer, that's the real number
    # (offer_price is a guaranteed instant sale, current_price is just
    # today's listed value, never actually paid to you as-is).
    sell_pool.loc[:, 'effective_price'] = sell_pool['offer_price'].where(sell_pool['has_offer'], sell_pool['current_price'])
    sell_pool.loc[:, 'profit'] = sell_pool['effective_price'] - sell_pool['buy_price']
    sell_pool.loc[:, 'profit_pct'] = (sell_pool['profit'] / sell_pool['buy_price']).where(sell_pool['buy_price'] > 0)
    sell_pool.loc[:, 'injured'] = sell_pool['status'].fillna('').str.startswith(('Injured', 'Doubtful'))
    # An offer existing isn't itself the signal — biwenger.as.com/market/
    # offers shows one of these for basically every squad player (its own
    # algorithmic "instant sale" price), and it's routinely BELOW the
    # listed price, not just above. What matters is whether it's actually
    # a good deal: offer_premium_pct compares it to today's market price
    # (positive = the guaranteed offer beats what he's listed for;
    # meaningfully negative = a discount not worth taking, i.e. "wait for
    # a better one" rather than an automatic reason to sell).
    sell_pool.loc[:, 'offer_premium_pct'] = (
        (sell_pool['offer_price'] - sell_pool['current_price']) / sell_pool['current_price'] * 100
    ).where(sell_pool['has_offer'] & (sell_pool['current_price'] > 0))
    sell_pool.loc[:, 'offer_is_generous'] = sell_pool['has_offer'] & (sell_pool['offer_premium_pct'] >= 3)
    sell_pool.loc[:, 'offer_is_lowball'] = sell_pool['has_offer'] & (sell_pool['offer_premium_pct'] < -3)

    # A good offer alone doesn't mean sell — a productive starter is
    # still worth keeping for the points, not just the cash (the goal is
    # points AND money). talent_keep is how much output he'd be taking
    # with him, expressed the same "more = more sellable" direction as
    # every other component (100 - normalize(talent)) so a proven
    # performer pulls the score DOWN regardless of how good the offer
    # looks, instead of a good offer unconditionally maxing the score.
    sell_pool.loc[:, 'blended_pts'] = _blended_pts(sell_pool, rounds_played)
    sell_pool.loc[:, 'talent_keep'] = 100 - _normalize(sell_pool['blended_pts'])
    sell_pool.loc[:, 'score'] = (
        0.25 * _normalize(sell_pool['bench_score']) +
        0.20 * _normalize(sell_pool['profit_pct'].fillna(0).clip(lower=0)) +
        0.10 * sell_pool['injured'].map({True: 100.0, False: 0.0}) +
        0.20 * _normalize(sell_pool['offer_premium_pct'].fillna(0)) +
        0.25 * sell_pool['talent_keep']
    ).round(1)

    # Positional depth veto: don't recommend selling a player if he's
    # currently the ONLY viable (start_pct >= 40) cover at a position he
    # plays — no offer or profit is worth leaving a lineup hole. Counts
    # are taken before any hypothetical sale, per position, across the
    # whole squad.
    viable_by_position = {'GK': 0, 'DEF': 0, 'MID': 0, 'FWD': 0}
    for _, row in sell_pool[sell_pool['start_pct'] >= 40].iterrows():
        for token in str(row['position']).split('/'):
            label = POSITION_LABELS.get(token.strip())
            if label:
                viable_by_position[label] += 1

    def _leaves_thin(position_str, start_pct):
        if start_pct < 40:
            return False  # wasn't part of the viable count to begin with
        tokens = [POSITION_LABELS.get(t.strip()) for t in str(position_str).split('/')]
        return any(viable_by_position.get(t, 0) <= 1 for t in tokens if t)

    sell_pool.loc[:, 'leaves_thin'] = sell_pool.apply(
        lambda r: _leaves_thin(r['position'], r['start_pct']), axis=1
    )

    # Only surface sells that actually make sense: an offer that beats
    # market value (worth grabbing even for a player you weren't
    # otherwise planning to move), meaningful banked profit on a player
    # who isn't nailed to the starting XI, a clear loss worth cutting
    # before it drops further, or bench fodder with no games and no cost
    # basis to protect. A nailed-on starter (>=70% start odds) only
    # qualifies if the payday is large enough to outweigh the points
    # they'd score, or the going-rate offer itself is what's compelling —
    # and never if it would leave a position with no viable cover at all.
    profit_pct = sell_pool['profit_pct']
    is_starter = sell_pool['start_pct'] >= 70
    worth_selling = (
        sell_pool['offer_is_generous']
        | (profit_pct.notna() & (profit_pct >= 0.15) & ~is_starter)
        | (profit_pct.notna() & (profit_pct <= -0.15))
        | (sell_pool['buy_price'].isna() & (sell_pool['start_pct'] <= 20))
        | (profit_pct.notna() & (profit_pct >= 0.5))
    ) & ~sell_pool['leaves_thin']
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

    # Funding candidates draw from the WHOLE roster, not just today's sell
    # recommendations — restricting to that short list meant a modest
    # shortfall with no small "worth selling anyway" candidates available
    # had nowhere to go but a star player (e.g. suggesting Pedri to cover
    # a 1.3M gap, wildly disproportionate — "overkill", per the user).
    # Price ascending is the PRIMARY sort — cheapest, least-disruptive
    # sells get used first, which is what actually avoids overkill.
    # is_star is a hard-ish gate ahead of that (protect proven players
    # even if one happens to be cheap-ish relative to a big shortfall);
    # "already recommended for other reasons" is deliberately only a
    # tiebreaker, not a priority driver — a player being a good sell for
    # unrelated reasons (e.g. a generous offer) doesn't make him the
    # right size for THIS shortfall if he costs 10M and the gap is 1.4M.
    funding_pool = sell_pool.copy()
    funding_pool.loc[:, 'blended_pts'] = _blended_pts(funding_pool, rounds_played)
    funding_pool.loc[:, 'keep_value'] = funding_pool['blended_pts'] + funding_pool['start_pct'] * 0.5
    funding_pool.loc[:, 'already_recommended'] = funding_pool.index.isin(sell_recommendations.index)
    # Top quartile of keep_value on the squad = a "star" — someone worth
    # actively protecting, only tapped if nothing smaller covers the gap.
    star_cutoff = funding_pool['keep_value'].quantile(0.75) if len(funding_pool) else 0
    funding_pool.loc[:, 'is_star'] = funding_pool['keep_value'] >= star_cutoff
    funding_pool = funding_pool.sort_values(
        by=['is_star', 'current_price', 'already_recommended'],
        ascending=[True, True, False]
    )
    funding_candidates = funding_pool[['player', 'current_price', 'keep_value', 'is_star']].to_dict('records')
    non_star_candidates = [c for c in funding_candidates if not c['is_star']]
    raisable_from_sells = float(funding_pool['current_price'].sum())

    def _funding_plan(shortfall):
        """Fewest players first, and — among combinations of that size
        that clear the shortfall — the one giving up the LEAST keep_value
        (talent + how nailed-on to start), not the one costing the
        fewest euros. Cheapest-first was the earlier version of this and
        it picked purely by price: e.g. it once chose a 92-last-season-
        point squad player over several near-zero-output ones just
        because he happened to be the cheapest single option, which
        undersells exactly what "worth keeping" means. Stars (top
        quartile of squad keep_value) are excluded from this search
        entirely — never offered as an option — up to 3 players; only
        the last-resort fallback below will ever reach for one, and only
        if nothing smaller-scale works at all."""
        if shortfall <= 0:
            return {'names': '', 'details': [], 'requires_star': False}

        for group_size in (1, 2, 3):
            best_combo, best_keep = None, None
            for combo in combinations(non_star_candidates, group_size):
                total_price = sum(c['current_price'] for c in combo)
                if total_price < shortfall:
                    continue
                total_keep = sum(c['keep_value'] for c in combo)
                if best_keep is None or total_keep < best_keep:
                    best_combo, best_keep = combo, total_keep
            if best_combo is not None:
                details = [{'player': c['player'], 'price': c['current_price'], 'is_star': bool(c['is_star'])} for c in best_combo]
                return {
                    'names': ', '.join(d['player'] for d in details),
                    'details': details,
                    'requires_star': any(d['is_star'] for d in details),
                }

        # Nothing up to 3 players covers it — fall back to the ordered
        # walk (cheapest/most-expendable first) regardless of how many
        # that takes.
        total, details = 0.0, []
        for c in funding_candidates:
            if total >= shortfall:
                break
            details.append({'player': c['player'], 'price': c['current_price'], 'is_star': bool(c['is_star'])})
            total += c['current_price']
        return {
            'names': ', '.join(d['player'] for d in details),
            'details': details,
            'requires_star': any(d['is_star'] for d in details),
        }

    buy_recommendations = buy_recommendations.copy()
    buy_recommendations.loc[:, 'shortfall'] = (buy_recommendations['suggested_bid'] - my_balance).clip(lower=0)
    buy_recommendations.loc[:, 'raisable_from_sells'] = raisable_from_sells
    buy_recommendations.loc[:, 'funded_without_hard_choices'] = buy_recommendations['shortfall'] <= raisable_from_sells
    plans = buy_recommendations['shortfall'].apply(_funding_plan)
    buy_recommendations.loc[:, 'funding_plan'] = plans.apply(lambda p: p['names'])
    buy_recommendations.loc[:, 'funding_details'] = plans.apply(lambda p: p['details'])
    buy_recommendations.loc[:, 'funding_requires_star'] = plans.apply(lambda p: p['requires_star'])

    # The score so far only judges the player himself — it's blind to
    # what funding the bid actually costs. Selling a pile of players (or
    # ones with real point potential) to afford a bid is a worse deal
    # than the same bid funded from cash on hand, and could leave the
    # rest of the squad unable to field a full, credible XI — that has
    # to feed back into the score, not just sit in a side panel. Broken
    # into visible parts ("desgranable") rather than one opaque number:
    # how many players, how many points those players would have scored,
    # and whether the remaining squad can still field a real lineup.
    def _funding_impact(details):
        if not details:
            return {'players_sold': 0, 'points_given_up': 0.0, 'leaves_squad_thin': False}
        names = {d['player'] for d in details}
        sold = funding_pool[funding_pool['player'].isin(names)]
        remaining = funding_pool[~funding_pool['player'].isin(names)]
        points_given_up = float(sold['blended_pts'].sum())
        # Rough "can still field 11" proxy: at least one viable GK, and
        # at least 10 more viable (start_pct >= 40) outfield players
        # left across the rest of the squad. An approximation, not an
        # exact lineup-legality check (formations vary) — but a squad
        # that fails this is clearly in trouble.
        viable_after = {'GK': 0, 'DEF': 0, 'MID': 0, 'FWD': 0}
        for _, row in remaining[remaining['start_pct'] >= 40].iterrows():
            for token in str(row['position']).split('/'):
                label = POSITION_LABELS.get(token.strip())
                if label:
                    viable_after[label] += 1
        leaves_squad_thin = viable_after['GK'] == 0 or sum(viable_after.values()) < 11
        return {
            'players_sold': len(names),
            'points_given_up': points_given_up,
            'leaves_squad_thin': leaves_squad_thin,
        }

    impact = buy_recommendations['funding_details'].apply(_funding_impact)
    buy_recommendations.loc[:, 'funding_players_sold'] = impact.apply(lambda i: i['players_sold'])
    buy_recommendations.loc[:, 'funding_points_given_up'] = impact.apply(lambda i: i['points_given_up'])
    buy_recommendations.loc[:, 'funding_leaves_squad_thin'] = impact.apply(lambda i: i['leaves_squad_thin'])
    buy_recommendations.loc[:, 'funding_penalty'] = (
        0.6 * _normalize(buy_recommendations['funding_points_given_up']) +
        0.4 * _normalize(buy_recommendations['funding_players_sold'])
    ) * 0.3  # secondary adjustment, not the dominant factor in the score

    # A candidate whose own price is climbing fast has real upside that
    # offsets some of what funding him costs — today's rise is money on
    # the table for as long as it holds. Discount the penalty for that,
    # but only partially (capped at 40%) and visibly (momentum_discount_pct
    # is its own field, not folded away): one day's price movement is a
    # real signal, not a forecast, and shouldn't erase a genuine funding
    # cost just because a player looks hot today.
    buy_recommendations.loc[:, 'momentum_discount_pct'] = (
        (_normalize(buy_recommendations['momentum_pct']) / 100).clip(lower=0, upper=1) * 40
    ).round(1)
    buy_recommendations.loc[:, 'funding_penalty'] = (
        buy_recommendations['funding_penalty'] * (1 - buy_recommendations['momentum_discount_pct'] / 100)
    )
    # Leaving the squad unable to field 11 isn't a matter of degree — no
    # amount of momentum makes that an acceptable trade.
    buy_recommendations.loc[buy_recommendations['funding_leaves_squad_thin'], 'funding_penalty'] = 100.0
    buy_recommendations.loc[:, 'score'] = (
        buy_recommendations['score'] - buy_recommendations['funding_penalty']
    ).clip(lower=0).round(1)
    # Re-apply the same quality bar now that funding cost is factored in
    # — a bid that only cleared 40 before accounting for what it costs to
    # fund isn't actually a real deal once that's priced in.
    buy_recommendations = buy_recommendations[buy_recommendations['score'] >= 40].sort_values('score', ascending=False)

    return buy_recommendations, sell_recommendations
