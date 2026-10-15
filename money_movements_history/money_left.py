import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
from post_identity import TEAM_NAME_ALIASES, _post_identity  # noqa: E402

# -----------------------
# Config / input
# -----------------------
INPUT_JSON = "../unique_posts.json"
OUT_DIR = Path("ledgers")  # folder to write per-team JSONs (created if missing)

# -----------------------
# Helpers
# -----------------------
money_pattern_inline = re.compile(r"([\d\.]+(?:,\d{3})*|\d+)\s*€")
bids_pattern_inline = re.compile(r"^(\d+)\s*licitacions")
ROLE_TOKENS = {"PT","DF","MC","DV","E","/"}

# ---- Post chronology (see _post_sort_key's docstring for why this exists) ----
_CATALAN_MONTHS = {
    'gen': 1, 'gener': 1, 'feb': 2, 'febrer': 2, 'mar': 3, 'març': 3,
    'abr': 4, 'abril': 4, 'maig': 5, 'jun': 6, 'juny': 6,
    'jul': 7, 'juliol': 7, 'ag': 8, 'agost': 8, 'set': 9, 'setembre': 9,
    'oct': 10, 'octubre': 10, 'nov': 11, 'novembre': 11, 'des': 12, 'desembre': 12,
}
_ABS_DATE_RE = re.compile(r"^(\d{1,2}) d['’]?e?\.?\s?([a-zçA-ZÇ]+)\.?(?:\s(\d{4}))?$")
# Matches singular and plural units and "una"/"un" as the count: "Fa 2
# hores", "Fa una hora", "fa 3 dies" (the round-result posts render it
# lowercase). The original pattern only matched "Fa <digits> hora|dia...",
# so every plural and every "una" fell through to the undated bucket.
_RELATIVE_UNIT_RE = re.compile(r"^[Ff]a (\d+|una?) (hor|di|setman|mes)")
_UNIT_TO_TIMEDELTA = {
    'hor': lambda n: timedelta(hours=n),
    'di': lambda n: timedelta(days=n),
    'setman': lambda n: timedelta(weeks=n),
    'mes': lambda n: timedelta(days=30 * n),  # approximate; only used to order posts, not for exact dates
}

def _post_sort_key(post, now):
    """Best-effort real timestamp for a scraped post, for sorting the
    ledger into true chronological order before FIFO-matching sales to
    purchases in compute_trades.

    Why this exists: compute_trades used to assume unique_posts.json's own
    scrape order WAS chronological order (just reversed or not). It isn't,
    reliably — confirmed live: a real purchase dated "3 d'ag." sits at file
    index 39, its matching sale dated "15 d'ag." sits at index 1017, and
    39 < 1017 in a file whose first post is "Fa una hora" and last is "26
    de jul." (i.e. newest-to-oldest overall) — meaning the file's own
    position wrongly implies the Aug 3 purchase is NEWER than the Aug 15
    sale that came after it. Post order drifts out of true chronology
    after enough incremental scroll-scrapes and merges across many runs;
    trusting file position for FIFO silently broke effectively every
    realized trade (buy_price=None) rather than a handful.

    Absolute dates ("15 d'ag.", optionally with a year) resolve to the
    most recent past occurrence of that day/month when no year is given —
    this is a genuinely long-running league (some posts DO carry an
    explicit year, e.g. "8 d'oct. 2025"), so a year-less date is
    ambiguous across years, not just months; "most recent match not after
    `now`" is the standard resolution and is exact whenever a year IS
    present. Relative tokens (Avui/Ahir/Fa X hores...) resolve against
    `now` directly. A post with no recognizable date token at all sorts
    as the oldest possible entry rather than crashing — better to put an
    unparseable post in a plausible-but-wrong place than to lose it.
    """
    for x in post:
        if not isinstance(x, str):
            continue
        tok = x.strip()
        if tok == 'Ara mateix':
            return now
        if tok == 'Avui':
            return now.replace(hour=12, minute=0, second=0, microsecond=0)
        if tok == 'Ahir':
            return (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        if tok == 'Demà':
            return (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        m = _RELATIVE_UNIT_RE.match(tok)
        if m:
            n = 1 if m.group(1) in ('un', 'una') else int(m.group(1))
            unit = m.group(2)
            return now - _UNIT_TO_TIMEDELTA[unit](n)
        m = _ABS_DATE_RE.match(tok)
        if m:
            day = int(m.group(1))
            month = _CATALAN_MONTHS.get(m.group(2).lower())
            if month is None:
                continue
            if m.group(3):
                try:
                    return datetime(int(m.group(3)), month, day)
                except ValueError:
                    continue
            # No year given: the most recent past (or today's) occurrence
            # of this day/month, walking back a year at a time in the
            # rare case the naive current-year guess is still in the
            # future (e.g. parsing a January post in February).
            for year in range(now.year, now.year - 5, -1):
                try:
                    candidate = datetime(year, month, day)
                except ValueError:
                    continue
                if candidate <= now:
                    return candidate
            continue
    return datetime.min

def parse_amount(amount_str: str) -> float:
    """Parse '1.234.567 €' style strings with NBSPs into float euros."""
    s = amount_str.replace("€", "").replace(" ", "").strip()
    s = s.replace(".", "")   # thousands
    s = s.replace(",", ".")  # decimal
    return float(s)

def find_subject_before(idx: int, post: list) -> str:
    """Find plausible player name before idx in post."""
    j = idx - 1
    while j >= 0:
        if isinstance(post[j], str):
            tok = post[j].strip()
            if tok and tok not in ROLE_TOKENS and "licitacions" not in tok and "Per " not in tok and "€" not in tok:
                return tok
        j -= 1
    return ""

_RENAME_RE = re.compile(r"^(.+?) ha canviat el seu nom a (.+)$")
_ROUND_PRIZE_RE = re.compile(r"^\t-?\d+ punts\t(.+€)")


def team_aliases(posts: list) -> dict:
    """old team name -> current name, from Biwenger's own "CANVI DE NOM"
    posts ("FerranGoaT ha canviat el seu nom a FreeJulian") plus the
    hand-maintained TEAM_NAME_ALIASES. Chains (A->B, then B->C) resolve
    straight to the latest name. Without this a renamed manager splits
    into two ledgers: everything before the rename under the old name
    (a ghost team with only a few weeks of salaries) and everything after
    under the new one (missing those same weeks).
    """
    aliases = dict(TEAM_NAME_ALIASES)
    for post in posts:
        if not post or post[0] != "CANVI DE NOM":
            continue
        for x in post:
            m = _RENAME_RE.match(x.strip()) if isinstance(x, str) else None
            if m and m.group(1).strip() != m.group(2).strip():
                aliases[m.group(1).strip()] = m.group(2).strip()
    resolved = {}
    for old in aliases:
        name, seen = old, set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        resolved[old] = name
    return resolved


def current_season_posts(posts: list, now=None) -> list:
    """The posts that belong in this season's money ledger: one copy of
    each real post, none older than the season-start credit.

    unique_posts.json holds both kinds of junk, and the ledger summed all
    of it (every team's balance came out tens of millions too low):
    - ~700 posts from previous seasons (back to 2023), pulled in when a
      post-layout change on 2026-09-22 made the scraper think it had
      never seen anything and scroll past the "Inici de joc" stop marker.
    - ~200 duplicate copies of the same post, saved before
      _post_identity learned to ignore the field that differed between
      them (date text, reaction count, team rename, layout), or differing
      in a re-rendered player name it can't ignore. Fixing the identity
      stops new duplicates but doesn't remove old ones.

    A post whose date can't be read is kept: in this file that's the
    round/salary posts dated "Jornada N" or a relative time, which are
    all from the live season — the pre-season backlog came in with
    absolute dates. The newest "Inici de joc" post marks the season start.
    """
    now = now or datetime.now()
    deduped, seen, seen_amounts = [], set(), set()
    for post in posts:
        key = _post_identity(post)
        if key in seen:
            continue
        # Second key for batch posts: the exact sequence of euro amounts.
        # Biwenger also re-renders player names ("Aimar" -> "Aimar Oroz")
        # and stray tokens ("!"), which no generic text identity can
        # strip — but two different posts listing the same 2+ amounts to
        # the euro, in the same order, don't happen. Single-amount posts
        # are left to _post_identity: one equal price is a real coincidence.
        amounts = tuple(x.strip() for x in post if isinstance(x, str) and "€" in x)
        amount_key = (post[0] if post else None, amounts)
        if len(amounts) >= 2 and amount_key in seen_amounts:
            continue
        seen.add(key)
        seen_amounts.add(amount_key)
        deduped.append(post)

    starts = [
        _post_sort_key(p, now) for p in deduped
        if any(isinstance(x, str) and x.strip() == 'Inici de joc' for x in p)
    ]
    starts = [d for d in starts if d != datetime.min]
    if not starts:
        return deduped
    season_start = max(starts)
    return [
        p for p in deduped
        if (d := _post_sort_key(p, now)) == datetime.min or d >= season_start
    ]


def compute_balances(posts: list) -> tuple[dict, dict]:
    """Compute each team's running balance and transaction ledger from the
    league forum posts (SOUS salaries, market sales/purchases, admin
    credits/penalties — including the season-start budget credit, which is
    just another admin credit entry, not special-cased).

    Sorted into true chronological order (oldest first) via
    _post_sort_key before processing — see its docstring for why file
    order can't be trusted. compute_balances's own running total is
    order-independent (it's just a sum), but ledger[team]'s ORDER is what
    compute_trades relies on for FIFO matching, so it has to be right
    here, once, rather than re-guessed downstream.

    Only this season's posts count, one copy each (current_season_posts),
    and a renamed team's old name is folded into its current one
    (team_aliases).

    Returns (balances, ledger): balances maps team display name -> float
    euros; ledger maps team display name -> list of transaction dicts,
    oldest first.
    """
    balances = defaultdict(lambda: 0)
    ledger = defaultdict(list)
    now = datetime.now()
    aliases = team_aliases(posts)
    posts = sorted(current_season_posts(posts, now), key=lambda p: _post_sort_key(p, now))

    for post in posts:
        # 1) Salaries (SOUS) — subtract
        if len(post) > 0 and post[0] == "SOUS":
            for line in post[5:]:
                if isinstance(line, str) and "\t" in line:
                    parts = [p for p in line.strip().split("\t") if p]
                    if len(parts) >= 2:
                        team, amount_str = parts[0].strip(), parts[1].strip()
                        amount = parse_amount(amount_str)
                        balances[team] -= amount
                        ledger[team].append({
                            "type": "Salary",
                            "movement": "Wages",
                            "amount": amount,
                            "signed_amount": -amount
                        })
                        continue

        # 2) Sales (Venut per … €) — add to header team
        if any(isinstance(x, str) and "Venut per" in x for x in post):
            header_team = post[1].strip() if len(post) > 1 and isinstance(post[1], str) else None
            for idx, item in enumerate(post):
                if isinstance(item, str) and "Venut per" in item:
                    m = money_pattern_inline.search(item)
                    if header_team and m:
                        amount = parse_amount(m.group(1))
                        subject = find_subject_before(idx, post)
                        balances[header_team] += amount
                        ledger[header_team].append({
                            "type": "Sale",
                            "movement": subject or "Player sale",
                            "amount": amount,
                            "signed_amount": +amount
                        })
                        continue

        # 3) Purchases (Fitxat per … Per … €) — subtract
        if any(isinstance(x, str) and "Fitxat per" in x for x in post):
            for idx, item in enumerate(post):
                if isinstance(item, str) and "Fitxat per" in item:
                    team = post[idx + 1].strip() if idx + 1 < len(post) and isinstance(post[idx + 1], str) else None
                    amount_str = post[idx + 2] if idx + 2 < len(post) and isinstance(post[idx + 2], str) else None
                    if team and amount_str:
                        m = money_pattern_inline.search(amount_str)
                        if m:
                            amount = parse_amount(m.group(1))
                            subject = find_subject_before(idx, post)
                            balances[team] -= amount
                            ledger[team].append({
                                "type": "Purchase",
                                "movement": subject or "Player purchase",
                                "amount": amount,
                                "signed_amount": -amount
                            })
                            continue

        # 4) Admin credits/penalties (includes the "Inici de joc" season-start
        # budget credit — it's just another line item here) — add
        if len(post) > 0 and post[0] == "ABONAMENTS I PENALITZACIONS":
            for line in post[5:]:
                if isinstance(line, str) and "\t" in line:
                    parts = [p for p in line.strip().split("\t") if p]
                    if len(parts) >= 2:
                        team, amount_str = parts[0].strip(), parts[1].strip()
                        amount = parse_amount(amount_str)
                        balances[team] += amount
                        ledger[team].append({
                            "type": "Admin adjustment",
                            "movement": "Credit/Penalty",
                            "amount": amount,
                            "signed_amount": +amount
                        })
                        continue

        # 5) Round prizes ("Fi de Jornada": every team's points and the
        # money it earned that round) — add. Never counted before, which
        # alone left every balance ~€1-3M short per round played.
        if len(post) > 0 and post[0] == "Fi de":
            for idx, item in enumerate(post):
                m = _ROUND_PRIZE_RE.match(item) if isinstance(item, str) else None
                if m and idx > 0 and isinstance(post[idx - 1], str):
                    team = post[idx - 1].strip()
                    amount = parse_amount(m.group(1))
                    balances[team] += amount
                    ledger[team].append({
                        "type": "Round prize",
                        "movement": post[1].strip() if len(post) > 1 and isinstance(post[1], str) else "Round prize",
                        "amount": amount,
                        "signed_amount": +amount
                    })

        # 6) Daily-streak bonus ("RATXA DIÀRIA": "+250.000 €", then every
        # team that earned it that day) — add.
        if len(post) > 0 and post[0] == "RATXA DIÀRIA":
            amount = None
            for item in post[2:]:
                if not isinstance(item, str) or not item.strip():
                    continue
                m = money_pattern_inline.search(item)
                if m:
                    amount = parse_amount(m.group(1))
                elif amount is not None:
                    team = item.strip()
                    balances[team] += amount
                    ledger[team].append({
                        "type": "Streak bonus",
                        "movement": "Daily streak",
                        "amount": amount,
                        "signed_amount": +amount
                    })

        # 7) Market sales/purchases (… Team, Mercat, Per … €) — add/subtract
        if 'FITXATGES' in post:
            for idx, item in enumerate(post):
                if isinstance(item, str) and item.strip() in ROLE_TOKENS and post[idx + 1] not in ROLE_TOKENS:
                    subject = post[idx + 1].strip() if idx + 1 < len(post) and isinstance(post[idx + 1], str) else None
                    seller_team = post[idx + 2].strip() if idx + 2 < len(post) and isinstance(post[idx + 2], str) else None
                    buyer_team = post[idx + 3].strip() if idx + 3 < len(post) and isinstance(post[idx + 3], str) else None
                    m = money_pattern_inline.search(post[idx + 4]) if idx + 4 < len(post) and isinstance(post[idx + 4], str) else None
                    if subject and m:
                        amount = parse_amount(m.group(1))
                        subject = find_subject_before(idx, post)
                        balances[seller_team] += amount
                        ledger[seller_team].append({
                            "type": "Sale",
                            "movement": subject or "Player sale",
                            "amount": amount,
                            "signed_amount": +amount
                        })
                        if buyer_team != 'Mercat':
                            balances[buyer_team] -= amount
                            ledger[buyer_team].append({
                                "type": "Purchase",
                                "movement": subject or "Player purchase",
                                "amount": amount,
                                "signed_amount": -amount
                            })

    merged_balances, merged_ledger = defaultdict(lambda: 0), defaultdict(list)
    for team, bal in balances.items():
        merged_balances[aliases.get(team, team)] += bal
    for team, entries in ledger.items():
        merged_ledger[aliases.get(team, team)].extend(entries)
    return dict(merged_balances), dict(merged_ledger)


def compute_trades(ledger: dict) -> dict:
    """Reconstruct buy/sell pairs per team from the ledger's Purchase/Sale
    entries, to compute realized profit on completed sales and unrealized
    profit on currently-held players that were actually bought (not part
    of the original free starting squad, which has no purchase record).

    ledger[team] entries are already oldest-first (compute_balances sorts
    them by real parsed date, see _post_sort_key), so a sale is matched
    here against the purchase that actually preceded it, FIFO, just by
    walking the list in order. Matching is by player name string, scoped
    per team — good enough for a first version, but two different real
    players who happen to share a short display name would collide; not
    handled.

    Returns {team: {"realized": [...], "open": {player_name: {"buy_price", "count"}}}}
    realized entries with buy_price=None mean a sale with no matching
    purchase in our window (e.g. part of the original squad, or the
    purchase happened before this season's scraped history starts).
    """
    results = {}
    for team, entries in ledger.items():
        chronological = entries
        open_positions = {}  # player -> FIFO queue of purchase prices
        realized = []
        for e in chronological:
            if e["type"] == "Purchase":
                open_positions.setdefault(e["movement"], []).append(e["amount"])
            elif e["type"] == "Sale":
                player = e["movement"]
                queue = open_positions.get(player)
                if queue:
                    buy_price = queue.pop(0)
                    realized.append({
                        "player": player,
                        "buy_price": buy_price,
                        "sell_price": e["amount"],
                        "profit": e["amount"] - buy_price,
                    })
                else:
                    realized.append({
                        "player": player,
                        "buy_price": None,
                        "sell_price": e["amount"],
                        "profit": None,
                    })

        open_summary = {}
        for player, queue in open_positions.items():
            if queue:
                open_summary[player] = {
                    "buy_price": sum(queue) / len(queue),
                    "count": len(queue),
                }
        results[team] = {"realized": realized, "open": open_summary}
    return results


def compute_bid_history(posts: list) -> list[dict]:
    """Extract per-transaction market bid activity (price paid vs. number of
    competing bids) from the same "Fitxat per" market-signing entries used
    by compute_balances()'s block 3, to later power a "how much should I
    bid" recommendation.

    Token layout confirmed against real posts before writing this (see the
    the class of bug documented in git history where offsets assumed for
    one post shape silently broke on the other): every "Fitxat per" entry
    is followed by [team, "Per N €", ...], and the item right after the
    price is either a "N licitacions" string or — when a signing drew no
    competing bids — that string is omitted entirely and the sequence
    jumps straight to the next stat/points number. There is no literal
    "0 licitacions" anywhere in the scraped data, confirming the omission
    is how zero-bid signings are represented rather than an inconsistent
    format. Since a missing token is ambiguous between "zero bids" and
    "some other omitted count", transactions without an explicit
    "N licitacions" string are skipped rather than guessed at — hence
    "one row per transaction that has a parseable bid count".

    The player name sits immediately before "Fitxat per" (post[idx-1]);
    walking further back collects the position token(s) that precede it.
    Positions are joined with "/" regardless of whether the raw scrape
    happened to include a literal "/" token between them (it does for
    two-position players but not for the rarer three-position ones, e.g.
    a real "DV","DF","MC" triple with no separator token in between) —
    normalizing here keeps the output format consistent either way.

    Returns a list of {"player": str, "price": float, "bids": int,
    "position": str|None} dicts, one per parseable transaction.
    """
    transactions = []
    for post in posts:
        if not any(isinstance(x, str) and "Fitxat per" in x for x in post):
            continue
        for idx, item in enumerate(post):
            if not (isinstance(item, str) and "Fitxat per" in item):
                continue

            amount_str = post[idx + 2] if idx + 2 < len(post) and isinstance(post[idx + 2], str) else None
            if not amount_str:
                continue
            m = money_pattern_inline.search(amount_str)
            if not m:
                continue
            price = parse_amount(m.group(1))

            bids = None
            if idx + 3 < len(post) and isinstance(post[idx + 3], str):
                bm = bids_pattern_inline.match(post[idx + 3].strip())
                if bm:
                    bids = int(bm.group(1))
            if bids is None:
                # No "N licitacions" token present — ambiguous, skip rather
                # than assume zero (see docstring).
                continue

            player = post[idx - 1].strip() if idx - 1 >= 0 and isinstance(post[idx - 1], str) else None
            if not player:
                continue

            position = None
            role_tokens = []
            j = idx - 2
            while j >= 0 and isinstance(post[j], str) and post[j].strip() in ROLE_TOKENS:
                tok = post[j].strip()
                if tok != "/":
                    role_tokens.append(tok)
                j -= 1
            if role_tokens:
                position = "/".join(reversed(role_tokens))

            transactions.append({
                "player": player,
                "price": price,
                "bids": bids,
                "position": position,
            })

    return transactions


# Price-range buckets for the bid-count aggregation below. Picked to give a
# reasonable spread against real league data (roughly six figures to eight
# figures of euros) rather than dumping most transactions into one bucket.
BID_BUCKETS = [
    (0, 500_000, "<500k"),
    (500_000, 1_000_000, "500k-1M"),
    (1_000_000, 3_000_000, "1M-3M"),
    (3_000_000, 5_000_000, "3M-5M"),
    (5_000_000, 10_000_000, "5M-10M"),
    (10_000_000, float("inf"), "10M+"),
]


def _bucket_label(price: float) -> str:
    for lo, hi, label in BID_BUCKETS:
        if lo <= price < hi:
            return label
    return BID_BUCKETS[-1][2]


def compute_bid_buckets(transactions: list[dict]) -> list[dict]:
    """Aggregate compute_bid_history() rows into price-range buckets with
    count/avg/median bid stats — a coarse first look at "how many bids does
    a player in this price range typically draw", ahead of a later
    recommender that presumably works from the raw per-transaction table
    for anything more precise.

    Returns a list of {"bucket": str, "count": int, "avg_bids": float,
    "median_bids": float, "min_price": float, "max_price": float} dicts,
    ordered as in BID_BUCKETS, omitting empty buckets.
    """
    by_bucket = defaultdict(list)
    for t in transactions:
        by_bucket[_bucket_label(t["price"])].append(t)

    rows = []
    for lo, hi, label in BID_BUCKETS:
        txs = by_bucket.get(label)
        if not txs:
            continue
        bids = sorted(t["bids"] for t in txs)
        n = len(bids)
        median = bids[n // 2] if n % 2 else (bids[n // 2 - 1] + bids[n // 2]) / 2
        prices = [t["price"] for t in txs]
        rows.append({
            "bucket": label,
            "count": n,
            "avg_bids": sum(bids) / n,
            "median_bids": median,
            "min_price": min(prices),
            "max_price": max(prices),
        })
    return rows


if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True, parents=True)

    with open(INPUT_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)

    balances, ledger = compute_balances(data)

    # Output: per-team JSON + combined CSV
    for team, entries in ledger.items():
        out = {
            "team": team,
            "starting_money": 0,
            "current_balance": balances[team],
            "transactions": entries
        }
        safe_name = "".join(c if c.isalnum() or c in (" ","-","_") else "_" for c in team).strip().replace(" ","_")
        with open(OUT_DIR / f"{safe_name}.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    rows = []
    for team, entries in ledger.items():
        for e in entries:
            rows.append({
                "Team": team,
                "Type": e["type"],
                "Movement": e["movement"],
                "Amount": e["amount"],
                "SignedAmount": e["signed_amount"]
            })

    df_tx = pd.DataFrame(rows, columns=["Team","Type","Movement","Amount","SignedAmount"])
    df_tx.to_csv("all_team_transactions.csv", index=False, encoding="utf-8")

    df_bal = pd.DataFrame(
        [{"Team": t, "RemainingMoney": bal} for t, bal in balances.items()]
    ).sort_values("RemainingMoney", ascending=False)
    df_bal.to_csv("remaining_money_per_team.csv", index=False, encoding="utf-8")

    print(df_bal)
    print("\nSample transactions:")
    print(df_tx.head(10))
