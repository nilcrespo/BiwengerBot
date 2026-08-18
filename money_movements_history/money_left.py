import json
import re
from collections import defaultdict
from pathlib import Path
import pandas as pd

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

def compute_balances(posts: list) -> tuple[dict, dict]:
    """Compute each team's running balance and transaction ledger from the
    league forum posts (SOUS salaries, market sales/purchases, admin
    credits/penalties — including the season-start budget credit, which is
    just another admin credit entry, not special-cased).

    Returns (balances, ledger): balances maps team display name -> float
    euros; ledger maps team display name -> list of transaction dicts.
    """
    balances = defaultdict(lambda: 0)
    ledger = defaultdict(list)

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

        # 5) Market sales/purchases (… Team, Mercat, Per … €) — add/subtract
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

    return dict(balances), dict(ledger)


def compute_trades(ledger: dict) -> dict:
    """Reconstruct buy/sell pairs per team from the ledger's Purchase/Sale
    entries, to compute realized profit on completed sales and unrealized
    profit on currently-held players that were actually bought (not part
    of the original free starting squad, which has no purchase record).

    ledger[team] entries are newest-first (the order posts were scraped
    in); processed oldest-first here (reversed) so a sale is matched
    against the purchase that actually preceded it, FIFO. Matching is by
    player name string, scoped per team — good enough for a first version,
    but two different real players who happen to share a short display
    name would collide; not handled.

    Returns {team: {"realized": [...], "open": {player_name: {"buy_price", "count"}}}}
    realized entries with buy_price=None mean a sale with no matching
    purchase in our window (e.g. part of the original squad, or the
    purchase happened before this season's scraped history starts).
    """
    results = {}
    for team, entries in ledger.items():
        chronological = list(reversed(entries))
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
