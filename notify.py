"""Sends a daily Telegram digest: buy/sell recommendations (via
recommenders.py, the same logic the dashboard uses) plus the outcome of
scraper.py's auto-renewal step, if it ran. Run after migration.py in the
daily GitHub Action, once the DB reflects today's scrape.

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as env vars — see
README/setup notes for how to obtain them. Missing/unset is treated as
"not configured yet", not an error: this prints a warning and exits
cleanly rather than failing the workflow.
"""
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

import pandas as pd

import recommenders

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
RENEWAL_RESULTS_PATH = "csvs/others/renewal_results.json"


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — skipping notification")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status == 200
        print("✅ Telegram message sent" if ok else f"⚠️ Telegram send returned HTTP {resp.status}")
        return ok
    except Exception as e:
        print(f"⚠️ Telegram send failed: {e}")
        return False


def _money(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if n < 0 else ""
    return f"{sign}€{abs(n):,.0f}"


def _renewal_section() -> str:
    """One line confirming the auto-renew step ran and succeeded — cheap
    peace of mind that listings aren't quietly lapsing, distinct from the
    buy/sell sections below which only appear when there's something
    worth acting on."""
    if not os.path.exists(RENEWAL_RESULTS_PATH):
        return ""
    try:
        with open(RENEWAL_RESULTS_PATH) as f:
            results = json.load(f)
    except Exception:
        return ""
    if not results:
        return ""

    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    line = f"🔄 <b>Renewed {len(ok)}/{len(results)} listings</b>"
    if failed:
        names = ", ".join(r.get("player_name") or str(r.get("player_id")) for r in failed[:5])
        line += f"\n  ⚠️ Failed: {names}"
    return line


def build_digest(conn, date) -> str:
    buy_df, sell_df = recommenders.build_recommendations(conn, date)

    sections = []

    renewal = _renewal_section()
    if renewal:
        sections.append(renewal)

    if len(buy_df):
        lines = [f"🎯 <b>Buy: {len(buy_df)} real deal(s)</b>"]
        for _, r in buy_df.head(5).iterrows():
            need = f" [{r['squad_need']}]" if r.get('squad_need') else ""
            afford = ""
            if r.get('shortfall', 0) > 0:
                if r.get('funding_plan'):
                    afford = f"\n    ⚠️ short {_money(r['shortfall'])} — would need to sell: {r['funding_plan']}"
                else:
                    afford = f"\n    ⚠️ short {_money(r['shortfall'])}, no sells available to cover it"
            lines.append(
                f"  • {r['name']} ({r['club']}, {r['position']}) — {_money(r['price'])}, "
                f"{r['probability']} start, score {r['score']}{need} → bid ~{_money(r['suggested_bid'])}{afford}"
            )
        sections.append("\n".join(lines))

    if len(sell_df):
        lines = [f"📤 <b>Sell: {len(sell_df)} candidate(s)</b>"]
        for _, r in sell_df.head(5).iterrows():
            tag = " 🔥 LIVE OFFER" if r.get('has_offer') else ""
            profit = _money(r['profit']) if pd.notna(r['profit']) else "—"
            lines.append(f"  • {r['player']} ({r['club']}) — profit {profit}, score {r['score']}{tag}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    conn = sqlite3.connect('data/biwenger_data.db')
    if not date:
        row = conn.execute("SELECT MAX(scraped_at) FROM market").fetchone()
        date = (row[0] or "")[:10]
    if not date:
        print("No scraped data yet — nothing to notify about")
        conn.close()
        return

    digest = build_digest(conn, date)
    conn.close()

    if not digest:
        print("Nothing noteworthy today — skipping Telegram message")
        return

    send_telegram_message(f"⚽ <b>Biwenger digest — {date}</b>\n\n{digest}")


if __name__ == "__main__":
    main()
