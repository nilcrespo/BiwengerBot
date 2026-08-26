"""Buy/sell recommendation logic, shared by the Flask dashboard (app.py)
and the standalone Telegram digest (notify.py) so the two never drift —
each just calls build_recommendations(conn, date) against its own DB
connection and gets back the same two ranked DataFrames.
"""
import unicodedata
from itertools import combinations
from statistics import NormalDist

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

# ---------- Bid competition model ----------
# Replaces the original flat "bucket markup" bid suggestion, which knew
# only one thing about a player: which price bracket he fell into. It had
# no idea WHO would compete for him, whether those managers could even
# afford him, or what real bids have historically looked like in euros
# (bid_history only ever recovers a bid *count* from the forum ledger —
# no amounts, no bidder identity).
#
# Three inputs now, in decreasing order of how much history backs them:
#   1. bid_history_buckets — the anchor. Hundreds of real signings,
#      bucketed by price, telling us how many bidders a player in this
#      bracket typically draws. Kept exactly as the prior it always was.
#   2. rival_lineups + team values — who could actually compete for THIS
#      player: can they afford him, and does their locked lineup show a
#      hole at his position or are they already stocked there. This
#      modulates the bucket prior up or down; it never replaces it.
#   3. market_bid_history — real euro amounts from the league board.
#      Calibrates how much each additional bidder is actually worth as a
#      premium over asking price. One day old at the time of writing, so
#      it's shrunk toward the old 2%/bidder assumption in proportion to
#      how many real transactions we have (see _markup_per_bidder).

# What each COMPETING bidder adds to the winning bid, as a fraction of
# asking price. This is the old _suggested_bid's own 2% assumption, moved
# from "per bidder" to "per bidder beyond the first" — a small,
# deliberate reduction, because the old reading implied a markup even on
# an uncontested signing and the captured data shows that isn't how it
# works: Biwenger's minimum bid IS the asking price, a lone bidder pays
# exactly that, and a real losing bid in the captured data landed on the
# market price to the euro.
#
# The "bidders" figure means the same thing in both data sources, which
# is worth stating because the model mixes them: the forum ledger's
# "7 licitacions" for the Miguel Sierra sale matches the board feed's
# winner + 6 losing bids exactly, and "2 licitacions" matches Bauzà's
# winner + 1. So bid_history.bids and market_bid_history.num_bidders both
# count the winner, and competing bidders is that number minus one.
FALLBACK_MARKUP_PER_BIDDER = 0.02
# Real transactions needed before the observed premium is trusted on its
# own. Below this, observed and fallback are blended proportionally
# rather than switching over at a cliff.
MIN_CALIBRATION_SAMPLES = 5
# The predicted range is "what if a couple more (or fewer) managers show
# up than expected" — a real, interpretable quantity, rather than an
# arbitrary ± percentage band.
#
# KNOWN LIMITATION (checked against 9 real auctions, 2026-08-22): this is a
# fixed constant, not learned from data, and it is too narrow to separate
# win_bid_50/75/90 in practice — on all 9 real auctions checked, the three
# win-probability bids either all won or all lost together, because a ±1.5
# bidder swing moves the price by only a few percent while two of the 9 real
# auctions (Miguel Sierra +73.6% over asking, Nacho Pérez +52.5%) were
# blowouts the bidder-count model can't reach at any reasonable win
# probability. Making this the observed spread of real outcomes (once
# there's enough of them to estimate a spread from, not just 9 points) is
# the natural next step — same "needs more volume first" situation as
# per-rival behavioral profiling.
BIDDER_UNCERTAINTY = 1.5
# A falling price is evidence THIS player isn't in demand, so the
# bracket-wide competition average doesn't apply to him (unchanged
# reasoning from the original _suggested_bid — it's what keeps an
# expensive, sliding player from being handed a big markup).
FALLING_PRICE_DAMPEN = 0.25
# A rising price isn't just a forecast that it'll keep rising — in
# Biwenger it's the demand signal itself, since prices move on how much
# the player is being bought and bid on. So it belongs in the expected
# BIDDER count too, not only in the price the bid resolves at. Applied
# only on the way up: on the way down FALLING_PRICE_DAMPEN above already
# encodes the (deliberately asymmetric) view that a sliding price means
# no competition worth paying for.
MOMENTUM_FULL_RISE_PCT = 0.05   # a 5%-in-a-day rise is as loud as this signal gets
MOMENTUM_FULL_RISE_EUR = 100_000  # ...and so is a €100k one
MOMENTUM_BIDDER_BOOST = 0.5     # a maxed-out rise draws at most 50% more bidders
MAX_EXPECTED_BIDDERS = 10
# Used only when a price bracket has no historical signings at all. 2.5
# bidders at the fallback 2%/bidder reproduces the flat 5% cushion the
# original _suggested_bid fell back to in exactly this situation.
NO_BUCKET_DATA_BIDDERS = 2.5

FORMATION_POSITION_ORDER = ('DEF', 'MID', 'FWD')


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _formation_requirements(formation):
    """'4-4-2' -> {'GK': 1, 'DEF': 4, 'MID': 4, 'FWD': 2}.

    Biwenger formations are always outfield-only and always in
    defender-midfielder-forward order (the goalkeeper is implicit), which
    matches how it orders `lineup.players` — verified live against every
    team in the league.
    """
    parts = [p for p in str(formation or '').split('-') if p.strip().isdigit()]
    if len(parts) != len(FORMATION_POSITION_ORDER):
        return None
    reqs = {'GK': 1}
    reqs.update({label: int(n) for label, n in zip(FORMATION_POSITION_ORDER, parts)})
    return reqs


SHORT_POSITIONS = frozenset(POSITION_LABELS.values())


def _player_position_labels(position_str):
    """Position tokens as GK/DEF/MID/FWD, accepting either the market
    table's long form ("Forward/Midfielder") or the short form Biwenger's
    own APIs use (rival_lineups, market_bid_history). Unknown tokens —
    notably "Coach", which the market really does list — drop out, and
    the caller treats an empty result as "no positional read on this
    one" rather than as "nobody needs him".
    """
    labels = []
    for token in str(position_str).split('/'):
        token = token.strip()
        label = POSITION_LABELS.get(token) or (token if token in SHORT_POSITIONS else None)
        if label:
            labels.append(label)
    return labels


class BidCompetitionModel:
    """Predicts a winning-bid RANGE for a market listing, given who in
    this specific league is actually in a position to compete for him.

    Built once per build_recommendations() call (it does a handful of
    queries), then asked for a suggestion per player. Also used directly
    by analysis/backtest_bid_model.py, which is why it's a module-level
    class rather than a closure inside build_recommendations.
    """

    def __init__(self, rivals, bucket_avg_bids, markup_per_bidder, calibration_samples):
        # rivals: [{'team_id', 'team_name', 'capacity', 'depth': {POS: float}}]
        self.rivals = rivals
        self.bucket_avg_bids = bucket_avg_bids
        self.markup_per_bidder = markup_per_bidder
        self.calibration_samples = calibration_samples

    # --- competitor weighting -------------------------------------------------
    @staticmethod
    def _afford_weight(capacity, price):
        """How freely a manager could bid on a player at this price.

        capacity is Biwenger's own max-bid limit — a credit line, not
        cash — so "can afford" isn't binary: a bid that would eat a
        manager's entire limit is one they're far less likely to actually
        make than one that costs them a fraction of it.
        """
        if not price or not capacity or capacity < price:
            return 0.0
        return 1.0 if capacity >= 2 * price else 0.5

    @staticmethod
    def _need_weight(depth):
        """How badly a manager needs another player at this position,
        from squad depth relative to what his own locked formation
        actually fields there (1.0 = exactly enough bodies to field it,
        2.0 = two full sets).

        A team already carrying two deep at a position is a much weaker
        bidder for a third than one running with no cover at all — but
        not a zero: managers do buy upgrades, and they buy to flip.
        """
        if depth is None:
            return 0.6  # lineup unknown for this team — stay neutral
        if depth < 1.35:
            return 1.0
        if depth < 2.0:
            return 0.6
        return 0.25

    def competitors(self, price, position_str):
        """Per-rival competition weight for this specific player, biggest
        threat first. A dual-position player is judged on whichever of
        his positions a given rival most needs."""
        labels = _player_position_labels(position_str)
        out = []
        for r in self.rivals:
            afford = self._afford_weight(r['capacity'], price)
            if not afford:
                continue
            needs = [self._need_weight(r['depth'].get(l)) for l in labels] or [0.6]
            weight = afford * max(needs)
            if weight > 0:
                out.append({'team_id': r['team_id'], 'team_name': r['team_name'], 'weight': weight})
        return sorted(out, key=lambda c: c['weight'], reverse=True)

    def expected_bidders(self, price, position_str, change=0.0):
        """How many managers are likely to bid (winner included), as the
        bracket's own historical average scaled by how contested THIS
        player looks and by how hard his price is currently rising.

        The bucket average stays the anchor deliberately: it's backed by
        hundreds of real signings, where the competitor model is backed
        by one round of lineups. Pressure of 0.5 — half the league
        plausibly interested, i.e. an unremarkable player — reproduces
        the bucket average exactly; a player every rival needs and can
        afford gets 1.5x it, one nobody can touch gets 0.5x.

        That 0.5x floor is deliberate rather than zero: rival capacity is
        a known LOWER bound (see build_bid_competition_model on why their
        balances can't be trusted), so "nobody can afford him" really
        means "nobody can afford him out of the credit line we can see",
        and the bracket's own history still says players at this price do
        get bid on.
        """
        bucket_avg = self.bucket_avg_bids.get(_bucket_label(price))
        base = bucket_avg if bucket_avg else NO_BUCKET_DATA_BIDDERS
        comps = self.competitors(price, position_str)
        if not self.rivals:
            pressure = 0.5  # no lineup data — fall back to the bucket average as-is
        else:
            pressure = sum(c['weight'] for c in comps) / len(self.rivals)
        # The rise has to be meaningful in BOTH relative and absolute
        # terms — hence the smaller of the two readings. Biwenger moves
        # prices in coarse steps, so on a cheap listing a single step is
        # already several percent: Bauzà's €30,000 move was +13% in a day
        # and drew all of two bidders, where the same percentage on a
        # mid-price player is a genuine stampede. Percentage alone
        # mistakes quantization for demand at the bottom of the market.
        momentum = 0.0
        if price and change > 0:
            momentum = min(change / price / MOMENTUM_FULL_RISE_PCT,
                           change / MOMENTUM_FULL_RISE_EUR, 1.0)
        bidders = base * (0.5 + pressure) * (1 + MOMENTUM_BIDDER_BOOST * momentum)
        return min(bidders, MAX_EXPECTED_BIDDERS), comps, pressure

    # --- bid range ------------------------------------------------------------
    def suggest(self, price, change, position_str):
        """Predicted winning-bid range for one listing, PLUS the bid
        actually worth acting on: the minimum euro amount needed to clear
        a chosen win probability.

        Why the range alone isn't the answer: checked against 9 real
        auctions (analysis/backtest_bid_model.py), every real listing here
        turned out to be a first-price sealed-bid auction — the winner
        pays exactly what they bid, never the runner-up's amount plus an
        increment (confirmed on all 9: winning_amount == the winner's own
        bid_amount, to the euro, every time). In a first-price auction,
        matching the historical AVERAGE clearing price is the wrong
        target — every euro bid above the true minimum needed to clear
        the field is pure waste, not a safety margin. A user who bid
        below both this model's and the old model's suggestion on 2 of 3
        real auctions still won both, €50,000-€80,000 cheaper than either
        model would have told them to pay. The range fields below are
        kept for backward compatibility (shortfall affordability checks
        key off suggested_bid_high); win_bid_50/75/90 are what should
        actually inform a bid decision.
        """
        price = float(price or 0)
        change = float(change) if pd.notna(change) else 0.0
        bidders, comps, pressure = self.expected_bidders(price, position_str, change)

        dampen = 1.0 if change > 0 else FALLING_PRICE_DAMPEN
        # A fast riser is likely still rising by the time a bid resolves,
        # so today's own growth is added on top of asking price rather
        # than being folded into a percentage of it.
        base = price + max(change, 0.0)

        def _at(n_bidders):
            # Bidders beyond the first: an uncontested signing goes for
            # the asking price, not the asking price plus a cushion.
            markup = max(n_bidders - 1, 0.0) * self.markup_per_bidder * dampen
            return base * (1 + markup)

        low = max(_at(bidders - BIDDER_UNCERTAINTY), price)
        high = _at(bidders + BIDDER_UNCERTAINTY)
        mid = _at(bidders)

        # Win-probability bids: treat the number of bidders who show up as
        # roughly Normal(mean=bidders, sd=BIDDER_UNCERTAINTY) — the same
        # spread already used for low/high — and invert it. Since _at() is
        # monotonic in n_bidders, "the bid that clears win probability p"
        # is just _at() evaluated at the bidder count whose CDF is p. A
        # bid can never need fewer than 1 bidder's worth (nobody signs for
        # under asking), so n is floored at 1 regardless of how low p is.
        bidder_dist = NormalDist(bidders, max(BIDDER_UNCERTAINTY, 1e-6))

        def _bid_for_win_prob(p):
            n_needed = max(bidder_dist.inv_cdf(min(max(p, 0.001), 0.999)), 1.0)
            return _at(n_needed)

        win_50 = _bid_for_win_prob(0.50)
        win_75 = _bid_for_win_prob(0.75)
        win_90 = _bid_for_win_prob(0.90)

        # The original flat €10,000 rounding collapses the whole range to
        # a single number on cheap players — a €210k listing's realistic
        # spread is a few thousand euros, which rounds away entirely and
        # renders as "€210,000 – €210,000". Bids are per-euro anyway, so
        # the rounding is only ever about not implying false precision.
        unit = 10_000 if price >= 1_000_000 else 1_000

        def _round(v):
            return round(v / unit) * unit

        return {
            'suggested_bid': _round(mid),
            'suggested_bid_low': _round(low),
            'suggested_bid_high': _round(high),
            'win_bid_50': _round(win_50),
            'win_bid_75': _round(win_75),
            'win_bid_90': _round(win_90),
            'expected_bidders': round(bidders, 1),
            'competitor_pressure': round(pressure, 2),
            'markup_per_bidder': round(self.markup_per_bidder, 4),
            'bid_calibration_samples': self.calibration_samples,
            'top_competitors': ', '.join(c['team_name'] for c in comps[:3]) or None,
            'competitor_count': len(comps),
        }


def _markup_per_bidder(conn, date):
    """How much each additional bidder actually adds to the winning bid,
    as a fraction of asking price, learned from real completed auctions.

    Only transactions first captured on or before `date` are used, so a
    backtest can ask what the model would have said at the time instead
    of quietly reading the answer off future data.

    Two approximations worth knowing about:
    - Asking price is reconstructed as (price at capture − that day's own
      price increment), since Biwenger bumps a player's price the day he
      sells. Checked against the real prior-day market listings for the
      transactions captured so far and it reproduced them to the euro
      (€1,440,000 and €230,000), so this is a sound reconstruction — but
      only of the price. The increment ITSELF is rewritten on the sale
      and can't be trusted as that day's momentum, which is why nothing
      here reads a price change off these rows.
    - The result is shrunk toward FALLBACK_MARKUP_PER_BIDDER in
      proportion to how few transactions back it. At the time of writing
      that's 2 auctions, which is not a sample — it's an anecdote, and it
      should not be allowed to overrule a bucket table built from
      hundreds of signings. As the board feed accumulates day by day, the
      observed number takes over on its own.
    """
    if not _table_exists(conn, 'market_bid_history'):
        return FALLBACK_MARKUP_PER_BIDDER, 0

    txns = pd.read_sql(
        """
        SELECT DISTINCT txn_key, player_price, price_change, winning_amount, num_bidders
        FROM market_bid_history
        WHERE is_winner = 1 AND DATE(scraped_at) <= ?
        """,
        conn, params=(date,)
    )
    if not len(txns):
        return FALLBACK_MARKUP_PER_BIDDER, 0

    asking = txns['player_price'] - txns['price_change'].fillna(0)
    # An uncontested signing (num_bidders == 1) carries no information
    # about what competition costs, and would divide by zero besides.
    usable = txns[(asking > 0) & (txns['num_bidders'] > 1) & txns['winning_amount'].notna()].copy()
    usable.loc[:, 'asking'] = asking[usable.index]
    if not len(usable):
        return FALLBACK_MARKUP_PER_BIDDER, 0

    competitors = usable['num_bidders'] - 1
    per_bidder = ((usable['winning_amount'] / usable['asking'] - 1) / competitors).clip(lower=0)
    # Median, not mean: one runaway auction shouldn't reset the league's
    # whole price level, and with a sample this small the mean is exactly
    # what one such auction would do.
    observed = float(per_bidder.median())
    n = len(usable)
    weight = min(n / MIN_CALIBRATION_SAMPLES, 1.0)
    return weight * observed + (1 - weight) * FALLBACK_MARKUP_PER_BIDDER, n


def build_bid_competition_model(conn, date, my_team_id=None):
    """Assemble the per-league inputs the bid model needs: every rival's
    spending capacity and positional depth, the historical bid-count
    buckets, and the euro calibration from real auctions.

    Degrades cleanly: with no rival_lineups rows (an older DB, or a date
    scraped before that capture existed) the competitor model contributes
    nothing and the bucket prior is used exactly as it was before.
    """
    d = resolve_scraped_at(conn, date)
    bid_buckets = pd.read_sql(
        "SELECT bucket, avg_bids, count FROM bid_history_buckets WHERE scraped_at LIKE ?",
        conn, params=(d,)
    )
    bucket_avg_bids = dict(zip(bid_buckets['bucket'], bid_buckets['avg_bids']))
    bucket_sample_size = dict(zip(bid_buckets['bucket'], bid_buckets['count']))

    markup, samples = _markup_per_bidder(conn, date)

    rivals = []
    if _table_exists(conn, 'rival_lineups'):
        lineups = pd.read_sql(
            """
            SELECT team_id, team_name, team_value, formation, slot, position
            FROM rival_lineups
            WHERE scraped_at = (SELECT MAX(scraped_at) FROM rival_lineups WHERE DATE(scraped_at) <= ?)
            """,
            conn, params=(date,)
        )
        for team_id, group in lineups.groupby('team_id'):
            if my_team_id is not None and team_id == my_team_id:
                continue
            reqs = _formation_requirements(group['formation'].iloc[0])
            squad = group['position'].value_counts().to_dict()
            depth = {}
            for label in ('GK', 'DEF', 'MID', 'FWD'):
                need = (reqs or {}).get(label)
                if need:
                    depth[label] = squad.get(label, 0) / need
            # Spending capacity. Deliberately NOT balance + 25% of squad
            # value, the formula dashboard_data.py uses for our own team:
            # the only balance available for a rival is the forum-ledger
            # reconstruction, and that is measurably wrong — migration.py's
            # own validation prints a €37M mismatch against the one team
            # whose real balance we can see. Feeding it in here would put
            # every rival tens of millions in the red and silently declare
            # that nobody in the league can afford anyone.
            #
            # 25% of squad value alone is instead a true LOWER bound on a
            # rival's max bid: Biwenger requires a non-negative balance
            # heading into each round, so their real limit is this plus
            # some cash we can't see. Under-crediting rivals is the safe
            # direction — it can only ever make the model predict less
            # competition than there really is, never invent competition
            # out of a broken number.
            capacity = 0.25 * float(group['team_value'].iloc[0] or 0)
            rivals.append({
                'team_id': team_id,
                'team_name': group['team_name'].iloc[0],
                'capacity': capacity,
                'depth': depth,
            })

    model = BidCompetitionModel(rivals, bucket_avg_bids, markup, samples)
    return model, bucket_avg_bids, bucket_sample_size


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
    # predicted winning-bid range from BidCompetitionModel — historical
    # bid counts for the price bracket, scaled by which rivals could
    # actually afford and need this specific player, priced in euros
    # against real observed auction premiums. ---
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

    # Who in this league would actually compete for a given player, and
    # what does a competing bidder really cost in euros — see
    # BidCompetitionModel. My own team is excluded from the rival set
    # (bidding against yourself isn't a thing).
    bid_model, bucket_avg_bids, bucket_sample_size = build_bid_competition_model(
        conn, date, my_team_id=my_team_id
    )

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
    # Predicted winning-bid range, one dict per listing, unpacked into
    # columns. Written this way (rather than one apply per field) so the
    # model is only asked once per player.
    bid_fields = ('suggested_bid', 'suggested_bid_low', 'suggested_bid_high',
                  'win_bid_50', 'win_bid_75', 'win_bid_90',
                  'expected_bidders', 'competitor_pressure', 'markup_per_bidder',
                  'bid_calibration_samples', 'top_competitors', 'competitor_count')
    bids = [bid_model.suggest(r['price'], r['change'], r['position']) for _, r in buy.iterrows()]
    for field in bid_fields:
        buy.loc[:, field] = [b[field] for b in bids]
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
    # injured was 0.10 — barely more than a rounding nudge next to bench
    # risk and talent. An injury/doubt is a concrete, near-term reason to
    # sell (he can't score points he doesn't play, on top of whatever his
    # start_pct already implies), not a marginal tiebreaker, so it's
    # doubled to 0.20, taken from profit_pct and talent_keep equally —
    # bench risk and offer quality are left untouched since neither one
    # is a proxy for this.
    sell_pool.loc[:, 'score'] = (
        0.25 * _normalize(sell_pool['bench_score']) +
        0.15 * _normalize(sell_pool['profit_pct'].fillna(0).clip(lower=0)) +
        0.20 * sell_pool['injured'].map({True: 100.0, False: 0.0}) +
        0.20 * _normalize(sell_pool['offer_premium_pct'].fillna(0)) +
        0.20 * sell_pool['talent_keep']
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
    # Affordability is checked against the TOP of the predicted range,
    # not its midpoint: the point of a range is that the high end is what
    # it takes to win a contested auction, and "✅ in balance" followed by
    # not actually being able to fund the bid you needed to place is the
    # exact failure this is meant to catch.
    buy_recommendations.loc[:, 'shortfall'] = (buy_recommendations['suggested_bid_high'] - my_balance).clip(lower=0)
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


# ---------- Best XI recommender ----------
# The five formations Biwenger actually allows (confirmed against every
# formation seen in real rival_lineups data: 4-4-2, 3-4-3, 3-5-2, 4-5-1,
# 4-3-3 — all five of Biwenger's known valid shapes turned up, none other
# did), as (DEF, MID, FWD) outfield counts alongside the fixed 1 GK.
BEST_XI_FORMATIONS = [(4, 4, 2), (4, 3, 3), (3, 5, 2), (3, 4, 3), (4, 5, 1)]


def _position_labels(position_str):
    return [
        POSITION_LABELS[t.strip()] for t in str(position_str).split('/')
        if POSITION_LABELS.get(t.strip())
    ]


def build_best_eleven(conn, date):
    """The best valid starting XI from the user's own squad: for each of
    Biwenger's 5 legal formations, fill GK/DEF/MID/FWD slots by expected
    value (blended talent x start probability — a great player who won't
    play contributes nothing), then keep whichever complete formation
    scores highest. The captain is whoever scores highest among the 11
    (Biwenger doubles the captain's points, so that's always the same
    player who'd be picked regardless of formation).

    Dual-position players (e.g. "Defender/Midfielder") are filled in two
    passes rather than solved as a true assignment problem: pass one
    fills each slot type from players whose FIRST listed position matches
    it (their primary registration), pass two uses leftover dual-eligible
    players to patch any slot a formation still can't fill from primaries
    alone. This is a heuristic, not a guaranteed-optimal solver — with a
    ~16-man squad and only two adjacent-position combinations possible in
    real football (DEF/MID, MID/FWD), a genuine conflict where the greedy
    order picks worse than optimal is rare enough not to justify a real
    bipartite-matching solver here, matching the rest of this module's
    "good heuristic over data we actually have" approach rather than a
    from-scratch optimizer.
    """
    d = resolve_scraped_at(conn, date)

    rounds_row = pd.read_sql(
        "SELECT MAX(played) AS rounds FROM team_players WHERE scraped_at LIKE ?",
        conn, params=(d,)
    )
    rounds_played = int(rounds_row['rounds'].iloc[0]) if len(rounds_row) and pd.notna(rounds_row['rounds'].iloc[0]) else 0

    roster = pd.read_sql(
        """
        SELECT tp.name AS player, tp.position, tp.club, tp.price, tp.status,
               tp.this_season_pts, tp.last_season_pts, tp.played
        FROM team_players tp
        JOIN team_balance tb ON tb.team_id = tp.team_id AND tb.is_me = 1 AND tb.scraped_at LIKE ?
        WHERE tp.scraped_at LIKE ?
        """,
        conn, params=(d, d)
    )
    if not len(roster):
        return {'formation': None, 'starters': [], 'bench': [], 'total_projected_points': 0}

    prob_df = pd.read_sql(
        "SELECT player_name, team_name, probability FROM player_probabilities "
        "WHERE scraped_at LIKE ? AND probability != '0%'",
        conn, params=(d,)
    )
    roster = attach_probabilities(roster, prob_df, name_col='player', club_col='club')
    roster.loc[:, 'start_pct'] = roster['probability'].apply(_parse_pct)
    # _blended_pts (used everywhere else) blends season TOTALS, which is
    # right for a buy/sell "how good is this player overall" score but
    # wrong here: a player with a full last season on record (30+ games)
    # would always dwarf one judged only on this season's 2 games so far,
    # regardless of actual per-match quality — caught live, a player with
    # 92 last-season points and zero appearances yet this year scored 15x
    # every other candidate's expected value on the same 0-100ish scale.
    # Blending PER-MATCH rates instead keeps every player on the same
    # scale. Last season's games-played isn't tracked (only this
    # season's `played` is), so a fixed 38-game season is the
    # approximation; this_season's rate is undefined (not just small)
    # with zero games played, not zero.
    LAST_SEASON_GAMES = 38
    this_ppm = (roster['this_season_pts'] / roster['played'].replace(0, pd.NA)).fillna(0)
    last_ppm = roster['last_season_pts'].fillna(0) / LAST_SEASON_GAMES
    progress = min(max(rounds_played, 0) / BLEND_TRANSITION_ROUNDS, 1.0)
    # THIS_WEIGHT_FLOOR exists to distrust a hot streak against a real,
    # longer track record (the buy recommender's job: rank overall
    # talent). Predicting THIS round's score is a different job — with
    # no real last season to shrink toward, there's nothing better to
    # fall back on, so suppressing this season's rate anyway just throws
    # away the only signal there is. Caught live: applying the floor
    # unconditionally (as if every player had a prior worth shrinking
    # toward) cut a full starting XI's total expected points roughly in
    # half versus using the real observed rate wherever no prior exists.
    has_last_season = roster['last_season_pts'] > 0
    this_weight = pd.Series(1.0, index=roster.index)
    this_weight[has_last_season] = THIS_WEIGHT_FLOOR + (1.0 - THIS_WEIGHT_FLOOR) * progress
    blended_ppm = last_ppm * (1.0 - progress) + this_ppm * this_weight
    # A player with literally zero recorded output in both fields hasn't
    # been observed to be bad — he just hasn't been observed. Leaving him
    # at exactly 0 makes "no data" beat a real, if poor, track record: a
    # trusted 90%-start regular coming off one rough match (a real
    # negative rate) lost his own starting slot to a 20%-start benchwarmer
    # with zero appearances ever, purely because 0 > negative. Imputing
    # the roster's own median rate (among players who DO have a real
    # rate) for the truly-unobserved ones keeps that comparison honest —
    # start_pct is still what mostly decides it, this just stops a
    # results-based over-punish from outweighing a much stronger,
    # directly-observed start-probability signal.
    has_signal = (roster['played'] > 0) | (roster['last_season_pts'] > 0)
    if has_signal.any():
        blended_ppm = blended_ppm.where(has_signal, blended_ppm[has_signal].median())
    # Two different numbers, deliberately kept apart. selection_value
    # prices in whether a player actually plays at all — a doubtful bench
    # risk shouldn't outrank a nailed-on starter just because he's more
    # talented when he does play — and drives which 11 get picked below.
    # projected_points is what's actually shown per player: his own
    # per-match rate, NOT discounted by start_pct again. A player already
    # selected into the XI has had his start risk priced in once, at
    # selection; showing his points discounted a second time understates
    # him for no reason — caught live: Eric García, one game played for 8
    # points (his real per-match rate), was shown scoring "4" once he'd
    # already been picked to start, because his 50% start_pct was applied
    # on top of a selection decision that had already accounted for it.
    roster.loc[:, 'projected_points'] = blended_ppm.round(2)
    roster.loc[:, 'selection_value'] = (blended_ppm * roster['start_pct'] / 100).round(2)
    roster.loc[:, 'labels'] = roster['position'].apply(_position_labels)
    roster.loc[:, 'primary'] = roster['labels'].apply(lambda ls: ls[0] if ls else None)

    best = None
    for def_n, mid_n, fwd_n in BEST_XI_FORMATIONS:
        needed = [('GK', 1), ('DEF', def_n), ('MID', mid_n), ('FWD', fwd_n)]
        assigned = []
        slot_of = {}  # player index -> which row (GK/DEF/MID/FWD) he fills

        def _take(pool, count, label):
            pool = pool[~pool.index.isin(assigned)].sort_values('selection_value', ascending=False)
            picked = pool.head(count).index.tolist()
            assigned.extend(picked)
            slot_of.update({i: label for i in picked})
            return picked

        filled = {}
        for label, count in needed:
            filled[label] = len(_take(roster[roster['primary'] == label], count, label))
        # Second pass: any formation slot a primary-only fill couldn't
        # complete gets patched from remaining dual-position-eligible
        # players (e.g. a MID slot short a body pulled from a leftover
        # Defender/Midfielder), best expected value first. His pitch row
        # is the SLOT he fills here, not his own primary position — a
        # Defender/Midfielder patched into a MID gap lines up with the
        # midfielders, not the defenders.
        for label, count in needed:
            shortfall = count - filled[label]
            if shortfall > 0:
                eligible = roster[roster['labels'].apply(lambda ls: label in ls)]
                _take(eligible, shortfall, label)

        if len(assigned) != 11:
            continue  # squad can't fill this formation at all — skip it
        # The formation itself is still picked on selection_value (start
        # risk has to matter when comparing formations — an 11 built
        # around iffy starters shouldn't win just because their ceiling
        # is high), even though projected_points is what gets displayed.
        total = roster.loc[assigned, 'selection_value'].sum()
        if best is None or total > best['total']:
            best = {'formation': f"{def_n}-{mid_n}-{fwd_n}", 'assigned': assigned, 'slot_of': slot_of, 'total': total}

    if best is None:
        return {'formation': None, 'starters': [], 'bench': [], 'total_projected_points': 0}

    # Captain is whoever's projected to outscore everyone else GIVEN he
    # plays — not discounted by his own start risk a second time, same
    # reasoning as projected_points itself.
    starters = roster.loc[best['assigned']].sort_values('projected_points', ascending=False)
    captain_idx = starters.index[0]
    starters = starters.assign(
        is_captain=starters.index == captain_idx,
        slot=[best['slot_of'][i] for i in starters.index],
    )
    bench = roster[~roster.index.isin(best['assigned'])].sort_values('projected_points', ascending=False)

    keep_cols = ['player', 'position', 'club', 'price', 'status', 'start_pct',
                 'projected_points', 'is_captain', 'slot']
    return {
        'formation': best['formation'],
        'starters': to_records(starters[keep_cols]),
        'bench': to_records(bench[[c for c in keep_cols if c not in ('is_captain', 'slot')]]),
        'total_projected_points': round(float(starters['projected_points'].sum()), 1),
    }
