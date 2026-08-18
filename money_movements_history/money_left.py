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
OUT_DIR.mkdir(exist_ok=True, parents=True)

# -----------------------
# Helpers
# -----------------------
money_pattern_inline = re.compile(r"([\d\.]+(?:,\d{3})*|\d+)\s*€")
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

# -----------------------
# Load data
# -----------------------
with open(INPUT_JSON, "r", encoding="utf-8") as f:
    data = json.load(f)

# -----------------------
# Ledgers + balances
# -----------------------
balances = defaultdict(lambda: 0)
ledger = defaultdict(list)

for post in data:
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


    # 5) Admin credits/penalties — add
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

    # 4) Market sales (… Team, Mercat, Per … €) — add
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
                    if buyer_team=='Mercat':
                        balances[seller_team] += amount
                        ledger[seller_team].append({
                            "type": "Sale",
                            "movement": subject or "Player sale",
                            "amount": amount,
                            "signed_amount": +amount
                            })
                    else:
                        balances[seller_team] += amount
                        ledger[seller_team].append({
                            "type": "Sale",
                            "movement": subject or "Player sale",
                            "amount": amount,
                            "signed_amount": +amount
                            })
                        balances[buyer_team] -= amount
                        ledger[buyer_team].append({
                            "type": "Purchase",
                            "movement": subject or "Player purchase",
                            "amount": amount,
                            "signed_amount": -amount
                            })
# -----------------------
# Output: per-team JSON + combined CSV
# -----------------------
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
