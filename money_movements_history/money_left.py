import json
import re
from collections import defaultdict
import pandas as pd

# --- Load data ---
with open("../unique_posts.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# --- Constants ---
START_MONEY = 60_000_000
money_pattern = re.compile(r"([\d\.]+(?:,\d{3})*|\d+)\s*€")

# --- Helper function to parse money strings ---
def parse_amount(amount_str):
    """Convert a money string like '1.234.567 €' into a float."""
    amount_str = amount_str.replace('€', '').replace(' ', '').strip()
    amount_str = amount_str.replace('.', '')
    amount_str = amount_str.replace(',', '.')
    return float(amount_str)

# --- Balances ---
balances = defaultdict(lambda: START_MONEY)

# --- Iterate over posts ---
for post in data:
    # Salaries (SOUS) → subtract from team balance
    if post[0] == "SOUS":
        for line in post[5:]:
            if isinstance(line, str) and '\t' in line:
                parts = line.strip().split('\t')
                if len(parts) >= 2:
                    team, amount_str = parts[0], parts[1]
                    balances[team] -= parse_amount(amount_str)

    # Player sold (Venut per ...)
    if any("Venut per" in str(item) for item in post):
        for item in post:
            if isinstance(item, str) and "Venut per" in item:
                match = money_pattern.search(item)
                if match:
                    amount = parse_amount(match.group(1))
                    team = post[1].strip()
                    balances[team] += amount

    # Player bought (Fitxat per ...)
    if any("Fitxat per" in str(item) for item in post):
        for idx, item in enumerate(post):
            if isinstance(item, str) and "Fitxat per" in item:
                if idx + 1 < len(post):
                    team = post[idx + 1].strip()
                    if idx + 2 < len(post):
                        amount_str = post[idx + 2]
                        if isinstance(amount_str, str) and "€" in amount_str:
                            match = money_pattern.search(amount_str)
                            if match:
                                amount = parse_amount(match.group(1))
                                balances[team] -= amount

    # Abonaments i penalitzacions → add to balance
    if post[0] == "ABONAMENTS I PENALITZACIONS":
        for line in post[5:]:
            if isinstance(line, str) and '\t' in line:
                parts = line.strip().split('\t')
                if len(parts) >= 2:
                    team, amount_str = parts[0], parts[1]
                    balances[team] += parse_amount(amount_str)

# --- Create DataFrame ---
df = pd.DataFrame(list(balances.items()), columns=["Team", "Remaining Money"])
df = df.sort_values(by="Remaining Money", ascending=False)

# --- Show result ---
print(df)

















import json
import re
from collections import defaultdict
from pathlib import Path
import pandas as pd

# -----------------------
# Config / input
# -----------------------
INPUT_JSON = "../unique_posts.json"
START_MONEY = 0
OUT_DIR = Path("ledgers")  # folder to write per-team JSONs (created if missing)
OUT_DIR.mkdir(exist_ok=True, parents=True)

# -----------------------
# Helpers
# -----------------------
money_pattern_inline = re.compile(r"([\d\.]+(?:,\d{3})*|\d+)\s*€")

ROLE_TOKENS = {"PT","DF","MC","DV","E","/"}

def parse_amount(amount_str: str) -> float:
    """Parse '1.234.567 €' style strings with NBSPs into float euros."""
    s = amount_str.replace("€", "").replace(" ", "").strip()  # NBSP
    s = s.replace(".", "")  # thousands
    s = s.replace(",", ".")  # decimal
    return float(s)

def find_subject_before(idx: int, post: list) -> str:
    """
    Walk backwards from index `idx` in `post` to find a plausible subject (player name).
    Skips role tokens and obvious markers.
    """
    j = idx - 1
    while j >= 0:
        if isinstance(post[j], str):
            tok = post[j].strip()
            # skip empty, role tokens, keywords, and obvious non-names
            if tok and tok not in ROLE_TOKENS and "licitacions" not in tok and "Per " not in tok and "€" not in tok:
                return tok
        j -= 1
    return ""  # fallback

# -----------------------
# Load data
# -----------------------
with open(INPUT_JSON, "r", encoding="utf-8") as f:
    data = json.load(f)

# -----------------------
# Ledgers + balances
# -----------------------
balances = defaultdict(lambda: START_MONEY)
ledger = defaultdict(list)  # team -> list of {type, subject, amount, signed_amount}

for post in data:
    # 1) Salaries (SOUS) — subtract
    if len(post) > 0 and post[0] == "SOUS":
        # entries start from index 5 onward in this scrape
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

    # 2) Sales (Venut per … €) — add to header team (post[1])
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

    # 3) Purchases (Fitxat per … | Per … €) — subtract from team that appears **after** "Fitxat per"
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

    # 4) Admin credits/penalties — add (positive values in the scrape)
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

# -----------------------
# Output: per-team JSON + combined CSV
# -----------------------
# 1) Per-team JSON ledgers
for team, entries in ledger.items():
    # also include current balance for convenience
    out = {
        "team": team,
        "starting_money": START_MONEY,
        "current_balance": balances[team],
        "transactions": entries
    }
    # safe file name
    safe_name = "".join(c if c.isalnum() or c in (" ","-","_") else "_" for c in team).strip().replace(" ","_")
    with open(OUT_DIR / f"{safe_name}.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

# 2) Combined CSV (all transactions, all teams)
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

# 3) Also write current balances
df_bal = pd.DataFrame(
    [{"Team": t, "RemainingMoney": bal} for t, bal in balances.items()]
).sort_values("RemainingMoney", ascending=False)
df_bal.to_csv("remaining_money_per_team.csv", index=False, encoding="utf-8")

# Optional: print quick summaries
print(df_bal)
print("\nSample transactions:")
print(df_tx.head(10))
