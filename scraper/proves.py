import pandas as pd
import re
from matplotlib import table
import pandas as pd
from playwright.sync_api import Playwright, sync_playwright, expect, TimeoutError
from typing import List, Dict

# Configuration
import os
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("BIWENGER_EMAIL")           # set as repo secret
PASSWORD = os.getenv("BIWENGER_PASSWORD")     # set as repo secret
HEADLESS = os.getenv("HEADLESS", "1") == "1"
MAX_RIVALS = 10  # Adjust based on your league size

def login(page):
    """Handle login process"""
    page.goto("https://biwenger.as.com/", wait_until="domcontentloaded",)
    try:
        page.get_by_role("button", name="Agree").click(timeout=5000)
    except:
        try:
            page.click("button#didomi-notice-agree-button", timeout=500)
        except:
            pass
    page.get_by_role("link", name="Comença a jugar!").click()
    page.get_by_role("button", name="Ja tinc un compte").click()
    page.get_by_role("textbox", name="Email").fill(EMAIL)
    page.get_by_role("textbox", name="Contrasenya").fill(PASSWORD)
    page.get_by_role("button", name="Iniciar sessió").click()
    try:
        page.get_by_role("button", name="Utilitza de forma gratuïta").click(timeout=500)
    except:
        try:
            page.locator("#cdk-overlay-0 > ng-component > button").click(timeout=500)
        except:
            pass


def safe_get_attribute(locator, name, default="", timeout=1000):
    """Safely get attribute with timeout handling"""
    try:
        locator.first.wait_for(state="attached", timeout=timeout)
        val = locator.first.get_attribute(name, timeout=timeout)
        return val.strip() if val else default
    except TimeoutError:
        return default

def normalize_player_name(name):
    """Normalize player names to match database format"""
    name = name.lower().strip()
    # Replace common accents and special characters
    replacements = {
        'ñ': 'n',
        'á': 'a',
        'é': 'e',
        'í': 'i',
        'ó': 'o',
        'ú': 'u',
        'ü': 'u',
        'à': 'a',
        'è': 'e',
        'ò': 'o',
    }
    for old, new in replacements.items():
        name = name.replace(old, new)
    return name.title()  # Capitalize first letters

def safe_inner_text(locator, default="", timeout=1000):
    """Safely get inner text with timeout handling"""
    try:
        locator.first.wait_for(state="visible", timeout=timeout)
        txt = locator.first.inner_text(timeout=timeout)
        # replace " for ' and remove accents
        txt = txt.strip()
        txt = txt.replace('"', '').replace('”', '')
        return normalize_player_name(txt)
    except TimeoutError:
        return default


def parse_money(s: str) -> float:
    if not s:
        return 0.0
    # keep digits, separators, sign; normalize to plain integer (these are usually whole €)
    t = re.sub(r"[^\d,.\-]", "", s)
    # remove thousands separators (commas or dots). if you actually have decimals, adapt this.
    t = t.replace(",", "").replace(".", "")
    return float(t or 0)

def extract_value_and_delta(cell):
    """
    cell: Locator pointing to the <td> that contains the value + <increment>.
    Returns (value_eur, delta_eur) where delta is negative if 'decrement' icon.
    """
    # 1) Base value: clone cell, remove <increment>, read remaining text
    base_text = cell.evaluate("""
        (td) => {
          const clone = td.cloneNode(true);
          clone.querySelectorAll('increment').forEach(n => n.remove());
          return clone.textContent.trim();
        }
    """)
    value_eur = parse_money(base_text)

    # 2) Delta from <increment>
    inc_label = cell.evaluate(
        "(td) => td.querySelector('increment')?.getAttribute('aria-label') || null"
    )
    cls = cell.evaluate(
        "(td) => td.querySelector('increment')?.className || ''"
    )
    delta = parse_money(inc_label) if inc_label else 0.0
    if "decrement" in cls:  # matches 'icon-decrement' or 'decrement'
        delta = -delta
    return value_eur, delta

def get_league_standings(page):
    print("\nExtracting league standings...")

    # Navigate to league standings
    page.goto("https://biwenger.as.com/league")
    links = []

    # Switch to table view
    try:
        page.get_by_role("button", name="Taula").click(timeout=3000)
    except:
        try:
            page.locator('i[role="button"][title="Table"]').click(timeout=3000)
        except Exception as e:
            print(f"Could not switch to table view: {e}")
            return pd.DataFrame()
    
    # Wait for table to load
    page.wait_for_selector("table tbody tr", timeout=10000)
    
    all_rows = []
    rows = page.locator("table tbody tr").all()
    
    for row in rows:
        try:
            # Extract position
            pos_locator = row.locator("user-position")
            raw_pos = pos_locator.inner_text()
            # only get numeral part of raw_pos
            position = re.search(r"\d+", raw_pos).group() if re.search(r"\d+", raw_pos) else "0"

            # Name
            name_cell = row.locator("td").nth(2).locator("a")
            name = safe_inner_text(name_cell, "Unknown Player")
            links.append(name_cell.get_attribute("href"))

            # Points data
            pts_cell = row.locator("td").nth(4).first
            points = pts_cell.inner_text().split()[0]

            # Team value and increment
            price_cell = row.locator("td").nth(5)  # Price column
            team_value, value_change = extract_value_and_delta(price_cell)
            
            # Num of players
            num_players_cell = row.locator("td").nth(6)
            num_players = safe_inner_text(num_players_cell, "0")

            all_rows.append({
                "position": position,
                "name": name,
                "points": points,
                "team_value": team_value,
                "value_change": value_change,
                "num_players": num_players,
                "scraped_at": pd.Timestamp.now().strftime("%Y-%m-%d")
            })
            
        except Exception as e:
            print(f"⚠️ Error processing market row: {e}")
            continue
    
    df = pd.DataFrame(all_rows)
    return df, links


from urllib.parse import urljoin
BASE = "https://biwenger.as.com"

def extract_team_players(page, team_link: str) -> pd.DataFrame:
    """
    Navigate directly to a team's page via /user/... link, switch to table view,
    and return the players table as a DataFrame.
    """
    # Go to the team page (absolute URL from relative /user/…)
    url = urljoin(BASE, team_link)
    page.goto(url, wait_until="domcontentloaded", timeout=30000)

    # Accept cookies if they pop up
    try:
        page.get_by_role("button", name=re.compile("ACEPT", re.I)).click(timeout=1500)
    except Exception:
        pass

    # Switch to the table view if such a button exists
    try:
        page.get_by_role("button", name=re.compile(r"Taula|Tabla|Table", re.I)).click(timeout=2000)
    except Exception:
        pass  # some pages are already in table view

    # Wait for the players table
    page.wait_for_selector("table.table.no-swipe", state="visible", timeout=8000)

    rows = page.locator("table.table.no-swipe tbody tr")
    records = []
    for i in range(rows.count()):
        row = rows.nth(i)
        try:
            # position (can be multiple badges)
            pos_items = row.locator("player-position")
            titles = []
            for j in range(pos_items.count()):
                t = pos_items.nth(j).get_attribute("title") or ""
                t = t.strip()
                if t:
                    titles.append(t)
            position = "/".join(titles) if titles else ""
            print(position)

            # club and player name
            club = safe_get_attribute(row.locator("a.team"), "title", "Unknown Club")
            name = safe_inner_text(row.locator("th a"), "Unknown Player")
            print(club, name)

            # points this season / last season (cell index 2 on current layout)
            pts_cell = row.locator("td").nth(2)
            this_season_pts = safe_inner_text(pts_cell.locator(":scope"), "0").split("\n")[0].strip()
            last_season_pts = safe_inner_text(pts_cell.locator("div"), "0").strip()
            print(this_season_pts, last_season_pts)
            if this_season_pts == 'Todinho':
                continue
            # price & change: the price cell usually has class "tr"
            price_cell = row.locator("td.tr").first
            price_eur, change_eur = extract_value_and_delta(price_cell)
            print(price_eur, change_eur)


            # status and misc
            status = safe_get_attribute(row.locator("player-status"), "title", "Unknown")
            played = safe_inner_text(row.locator("td").nth(6), "0")
            ppm = safe_inner_text(row.locator("td").nth(7), "0")

            # home / away cells (indices 8 and 9 on current layout)
            home_cell = row.locator("td").nth(8)
            home_pts = safe_inner_text(home_cell.locator(":scope"), "0").split("\n")[0].strip()
            home_avg = safe_inner_text(home_cell.locator("div.sub-item"), "0")

            away_cell = row.locator("td").nth(9)
            away_pts = safe_inner_text(away_cell.locator(":scope"), "0").split("\n")[0].strip()
            away_avg = safe_inner_text(away_cell.locator("div.sub-item"), "0")

            team_name = ' '.join(link.split('/')[2].split('-')[:-1]).capitalize()
            records.append({
                "team": team_name,
                "position": position,
                "club": normalize_player_name(club),
                "name": normalize_player_name(name),
                "this_season_pts": this_season_pts,
                "last_season_pts": last_season_pts,
                "price": price_eur,            # int euros
                "change": change_eur,          # int euros (negative on decrement)
                "status": status,
                "played": played,
                "points_per_match": ppm,
                "home_pts": home_pts,
                "home_average": home_avg,
                "away_pts": away_pts,
                "away_average": away_avg,
            })
        except Exception as e:
            print(f"⚠️ Error processing row {i}: {e}")
            continue

    df = pd.DataFrame(records)

    filename = f"csvs/teams/team_{team_name.replace(' ', '_').replace('/', '_')}.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Saved {len(df)} players to {filename}")
    return df


if __name__ == "__main__":
    links = ['/user/ferrangoat-12617705', '/user/patsi-f-c-7745504', '/user/raifc-11457758', '/user/locombia-fc-1716474', '/user/real-club-de-balompie-rafanells-11458316', '/user/general-hansi-topete-1715607', '/user/manchester-tity-1792970', '/user/hernan-krezzpo-12616837', '/user/cd-numancia-9892417', '/user/minabo-de-kiev-11458371']
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(
            locale="ca-ES",
            extra_http_headers={"Accept-Language": "ca-ES,ca;q=0.9"}
        )
        page = context.new_page()

        # Login
        login(page)
        # rival_teams, links = get_league_standings(page)
        # print(rival_teams)
        # print(links)
        for link in links:
            df = extract_team_players(page, link)
            print(df)
