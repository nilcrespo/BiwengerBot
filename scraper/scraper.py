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

# Biwenger's own /api/v2/auth/login rejects requests whose User-Agent
# reports "HeadlessChrome" (Playwright's default headless mode) with a
# 403 "Not allowed" before credentials are even checked. A normal desktop
# Chrome UA string gets through fine — confirmed against the live API.
DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# The card/table view toggle button used to always say "Taula" (Catalan).
# The app now renders some views in English ("Table") regardless of the
# ca-ES locale/Accept-Language we set, so match either.
TABLE_VIEW_LABEL = re.compile(r"Taula|Table", re.I)

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
    # Biwenger dropped the old "Ja tinc un compte" intermediate screen — the
    # /login page now goes straight to CREAR COMPTE / INICIAR SESSIÓ, and
    # clicking INICIAR SESSIÓ reveals the email/password form in place.
    page.get_by_role("button", name="Iniciar sessió").click()
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
    """Safely get attribute with timeout handling.

    Many callers probe for elements that are conditionally rendered
    (e.g. Angular *ngIf) and legitimately absent on most rows. Checking
    count() first (instant, no wait) avoids paying the full `timeout`
    on every row where the element simply isn't there — across a large
    table that difference is the gap between a few seconds and several
    minutes.
    """
    if locator.count() == 0:
        return default
    try:
        locator.first.wait_for(state="attached", timeout=timeout)
        val = locator.first.get_attribute(name, timeout=timeout)
        return val.strip() if val else default
    except TimeoutError:
        return default

def safe_inner_text(locator, default="", timeout=1000):
    """Safely get inner text with timeout handling (see safe_get_attribute)."""
    if locator.count() == 0:
        return default
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
        page.get_by_role("button", name=TABLE_VIEW_LABEL).click(timeout=3000)
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

            # Name — keep the raw display text (accents, casing) untouched here.
            # extract_team_players() needs to click a button whose accessible
            # name matches this exactly; normalizing it (as safe_inner_text
            # does) caused the same team to be scraped under two different
            # keys depending on whether the click matched.
            name_cell = row.locator("td").nth(2).locator("a")
            try:
                name_cell.first.wait_for(state="visible", timeout=1000)
                name = name_cell.first.inner_text(timeout=1000).strip()
            except TimeoutError:
                name = "Unknown Player"
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

            # Biwenger marks the logged-in user's own row with class="selected"
            row_class = row.get_attribute("class") or ""
            is_me = "selected" in row_class.split()

            all_rows.append({
                "position": position,
                "name": name,
                "points": points,
                "team_value": team_value,
                "value_change": value_change,
                "num_players": num_players,
                "is_me": is_me,
                "scraped_at": pd.Timestamp.now().strftime("%Y-%m-%d")
            })
            
        except Exception as e:
            print(f"⚠️ Error processing market row: {e}")
            continue
    
    df = pd.DataFrame(all_rows)
    df.to_csv("csvs/others/league_standings.csv", index=False)
    return df.to_dict('records'), links

def extract_team_players(page, team_name: str) -> pd.DataFrame:
    """Extract player data for a specific team"""
    print(f"\nExtracting players for {team_name}...")
    
    # Navigate to team page and click table view
    page.get_by_role("button", name=team_name).last.click()
    page.wait_for_timeout(500)  # let the team view settle before probing it

    # The toggle click can land before the view is fully ready and silently
    # no-op (no exception — the click succeeds, it just doesn't switch the
    # view in time), so retry once with a longer wait rather than trusting
    # a single attempt.
    for attempt, click_timeout in enumerate((3000, 5000)):
        try:
            page.get_by_role("button", name=TABLE_VIEW_LABEL).click(timeout=click_timeout)
        except Exception:
            pass
        try:
            page.wait_for_selector("table.table.no-swipe", timeout=5000)
            break
        except Exception:
            if attempt == 1:
                raise
            page.wait_for_timeout(500)
    
    all_rows = []
    rows = page.locator("table.table.no-swipe tbody tr").all()
    for row in rows:
        try:
            # Extract player data
            pos_locator = row.locator("player-position")
            pos_count = pos_locator.count()
            
            titles = [pos_locator.nth(j).get_attribute("title").strip() for j in range(pos_count)]
            position = "/".join(titles)
            
            club = safe_get_attribute(row.locator("a.team"), "title", "Unknown Club")
            name = safe_inner_text(row.locator("th a"), "Unknown Player")

            pts_cell = row.locator("td").nth(2)
            this_season_pts = safe_inner_text(pts_cell.locator(":scope").first, "0").split('\n')[0]
            last_season_pts = safe_inner_text(pts_cell.locator("div"), "0")

            price_raw = row.locator("td.tr").nth(0).inner_text().strip()
            price = re.sub(r"[^\d\.]", "", price_raw)

            # Same trap as the market's price-change field: aria-label uses
            # a Unicode minus sign ("−€10,000") on decreases, which a plain
            # ASCII-hyphen regex silently drops — every decrease was being
            # recorded as a positive number. Sign comes from the CSS class
            # instead, matching extract_value_and_delta()'s approach.
            change_label = row.evaluate("tr => tr.querySelector('increment')?.getAttribute('aria-label') || null")
            change_cls = row.evaluate("tr => tr.querySelector('increment')?.className || ''")
            change = parse_money(change_label) if change_label else 0.0
            if "decrement" in change_cls:
                change = -change

            status = safe_get_attribute(row.locator("player-status"), "title", "Unknown")
            played = safe_inner_text(row.locator("td").nth(6), "0")
            ppm = safe_inner_text(row.locator("td").nth(7), "0")

            home_cell = row.locator("td").nth(8)
            home_pts = safe_inner_text(home_cell.locator(":scope").first, "0").split('\n')[0]
            home_average = safe_inner_text(home_cell.locator("div.sub-item"), "0")

            away_cell = row.locator("td").nth(9)
            away_pts = safe_inner_text(away_cell.locator(":scope").first, "0").split('\n')[0]
            away_average = safe_inner_text(away_cell.locator("div.sub-item"), "0")

            all_rows.append({
                "team": team_name,
                "team_id": normalize_team_key(team_name),
                "position": position,
                "club": normalize_player_name(club),
                "name": normalize_player_name(name),
                "this_season_pts": this_season_pts,
                "last_season_pts": last_season_pts,
                "price": price,
                "change": change,
                "status": status,
                "played": played,
                "points_per_match": ppm,
                "home_pts": home_pts,
                "home_average": home_average,
                "away_pts": away_pts,
                "away_average": away_average,
            })
        except Exception as e:
            print(f"⚠️ Error processing row: {e}")
            continue
    
    df = pd.DataFrame(all_rows)
    team_key = normalize_team_key(team_name)
    filename = f"csvs/teams/team_{team_key}.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Saved {len(df)} players to {filename}")
    return df

def wait_for_table_ready(page, timeout_ms=20000, poll_ms=200):
    table = page.locator(".table-responsive.section-xs.light.ng-star-inserted table").first
    table.wait_for(state="visible", timeout=timeout_ms)

    # 1) wait for at least one row
    tbody_rows = page.locator(".table-responsive.section-xs.light.ng-star-inserted table tbody tr")
    page.wait_for_timeout(200)  # small grace
    total = 0
    elapsed = 0
    while elapsed < timeout_ms:
        total = tbody_rows.count()
        if total > 0:
            break
        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    if total == 0:
        raise TimeoutError("Table has no rows after waiting.")

    # 2) wait for skeletons to disappear (if they exist at all)
    skeletons = page.locator(".table-responsive.section-xs.light.ng-star-inserted table >> text=/█+/")
    elapsed = 0
    while elapsed < timeout_ms:
        if skeletons.count() == 0:
            break
        page.wait_for_timeout(poll_ms)
        elapsed += poll_ms

    return table  # a Locator pointing to <table>


def get_rival_teams(page) -> List[Dict]:
    """Get list of all rival teams in the league"""
    page.get_by_role("link", name="Lliga").click()    
    teams = []
    
    team_elements = page.locator("user-card").all()
    # Switch to table view
    try:
        page.get_by_role("button", name=TABLE_VIEW_LABEL).click(timeout=3000)
    except:
        try:
            page.locator('i[role="button"][title="Table"]').click(timeout=3000)
        except Exception as e:
            print(f"Could not switch to table view: {e}")
            return pd.DataFrame()
    # Wait for the table to be visible and stable
    table = wait_for_table_ready(page)
    html = table.evaluate("el => el.outerHTML")
    # if table is the locator for the <table> element:
    # build minimal valid table HTML and pass via StringIO
    # Parse with pandas
    try:
        from io import StringIO
        df = pd.read_html(StringIO(f"<table>{html}</table>"))[0]
    except Exception as e:
        print(f"Failed to parse table: {e}")
        return pd.DataFrame()
    
    df = df.rename(columns={
        'Unnamed: 0': 'position',
        'Unnamed: 2': 'name',
        'Punts': 'points',
        'Jugadors': 'players'
    })
    # Extract just the number from position (remove º∞)
    print(df)
    df['position'] = df['position'].str.extract(r'(\d+)').astype(int)

    # Split 'Equip' column into 'team_value' and 'team_growth'
    df[['team_value', 'eur', 'team_growth', 'eur2']] = df['Equip'].str.split(expand=True)

    # Drop unnecessary columns
    df = df.drop(columns=['Unnamed: 1', 'Unnamed: 3', 'Equip', 'eur', 'eur2'])

    # Convert monetary columns to numeric (remove € and . as thousand separator)
    df['team_value'] = df['team_value'].str.replace('€', '').str.replace('.', '').str.replace(',', '.').astype(float)
    df['team_growth'] = df['team_growth'].str.replace('€', '').str.replace('.', '').str.replace(',', '.').astype(float)

    # Reorder columns if needed
    df = df[['position', 'name', 'points', 'team_value', 'team_growth', 'players']]
    # print(len(team_elements), "teams found in league")
    
    # for team in team_elements:
    #     name = safe_inner_text(team.locator("h3 > a"), "Unknown Team")
    #     position = safe_inner_text(team.locator("user-position"), "0")
    #     points = safe_inner_text(team.locator("div.right ng-star-insterted"), "0")
    #     teams.append({
    #         "position": position,
    #         "name": name,
    #         "points": points.replace(" pl.", "")
    #     })
    
    # Save league standings
    df.to_csv("csvs/others/league_standings.csv", index=False)
    return df.to_dict('records')

def extract_all_players(page) -> pd.DataFrame:
    # Navigate
    page.goto("https://biwenger.as.com/players")
    try:
        page.get_by_role("button", name=TABLE_VIEW_LABEL).click()
    except:
        page.locator('i[role="button"][title="Table"]').click()

    all_rows = []

    # Wait for the table to show at least one row
    page.wait_for_selector("table tbody tr")

    while True:
        rows = page.locator("table tbody tr")
        players_shown = page.locator("span.summary.ng-star-inserted").inner_text()
        try:
            player_end   = int(players_shown.split("-")[1].split('de')[0].strip())
            total_players = int(players_shown.split("de")[1].strip())
        except:
            player_end = int(players_shown.split("-")[1].split('of')[0].strip())
            total_players = int(players_shown.split("of")[1].strip())
        print(f"Showing players until player {player_end} of {total_players}")
        for i in range(min(9, total_players-player_end)):
            row = rows.nth(i)
            try:
                # 1) Position
                pos_locator = row.locator("player-position")
                pos_count   = pos_locator.count()
                titles = [
                    pos_locator.nth(j)
                                .get_attribute("title")
                                .strip()
                    for j in range(pos_count)
                ]
                position = "/".join(titles)  # e.g. "Migcampista/Davanter"

                # 2) Club
                club = safe_get_attribute(row.locator("a.team"), "title", default="Unknown Club")


                # 3) Name
                name = safe_inner_text(row.locator("th a"), default="Unknown Player")

                # 4) Season vs Last‑Season points
                pts_cell = row.locator("td").nth(2)
                this_season_pts = safe_inner_text(pts_cell.locator(":scope").first, default="0").split('\n')[0]
                last_season_pts = safe_inner_text(pts_cell.locator("div"), default="0")

                # 5) Market price
                price_raw = row.locator("td.tr").nth(0).inner_text().strip()
                price = re.sub(r"[^\d\.]", "", price_raw)

                # 6) Δ since yesterday
                change_raw = row.locator("increment") \
                            .get_attribute("aria-label") \
                            .strip()
                change = re.sub(r"[^\d\.]", "", change_raw)

                # 7) Status
                status = row.locator("player-status") \
                            .get_attribute("title") \
                            .strip()

                # 8) Games played
                played = row.locator("td").nth(6).inner_text().strip()

                # 9) Points per match
                ppm = row.locator("td").nth(7).inner_text().strip()

                # 10) Home & Away averages
                home_cell = row.locator("td").nth(8)
                home_pts     = home_cell.locator(":scope").first.inner_text().strip().split('\n')[0]
                home_average = home_cell.locator("div.sub-item").inner_text().strip()

                away_cell = row.locator("td").nth(9)
                away_pts     = away_cell.locator(":scope").first.inner_text().strip().split('\n')[0]
                away_average = away_cell.locator("div.sub-item").inner_text().strip()

                all_rows.append({
                    "position":           position,
                    "club":               club,
                    "name":               name,
                    "this_season_pts":    this_season_pts,
                    "last_season_pts":    last_season_pts,
                    "price":              price,
                    "change":             change,
                    "status":             status,
                    "played":             played,
                    "points_per_match":   ppm,
                    "home_pts":           home_pts,
                    "home_average":       home_average,
                    "away_pts":           away_pts,
                    "away_average":       away_average,
                })
            except TimeoutError as e:
                # Log the index and a bit of context
                print(f"⚠️ Timeout on row {i}: “” — skipping this row.")
                continue

        # check for last page

        if total_players-player_end < 9:
            print(f"Reached the last page with {total_players} players.")
            break
        # otherwise go next
        page.get_by_text("›").click()
        ROW_WAIT_TIMEOUT = 5_000

        # wait for the table to show up, or break if it times out
        try:
            page.wait_for_selector("table tbody tr", timeout=ROW_WAIT_TIMEOUT)
        except TimeoutError:
            print(f"No rows appeared within {ROW_WAIT_TIMEOUT}ms—stopping pagination.")
            break
    
    df = pd.DataFrame(all_rows)
    df.to_csv("players.csv", index=False)
    print(f"Extracted {len(df)} players → players.csv")
    return df

def get_my_team_balance(page) -> dict:
    """Scrape the logged-in user's own cash balance from /team.

    This is Biwenger's own authoritative figure (the <balance> element in
    the squad-stats widget), used to sanity-check the forum-post-derived
    ledger balance for the user's own team — the ledger can be computed
    for every team from public posts, but this ground truth is only ever
    available for whichever team is logged in.
    """
    page.goto("https://biwenger.as.com/team", wait_until="domcontentloaded", timeout=15000)
    try:
        page.wait_for_selector("squad-stats balance", timeout=8000)
    except TimeoutError:
        print("⚠️ Could not find balance widget on /team")
        return {}

    raw = safe_inner_text(page.locator("squad-stats balance"), "")
    balance = parse_money(raw)

    manager_name = safe_inner_text(page.locator("a.avatar-container span, .user-name"), "")
    print(f"💶 My balance: €{balance:,.0f}")
    return {"balance": balance, "raw": raw, "scraped_at": pd.Timestamp.now().strftime("%Y-%m-%d")}

def extract_market_players(page) -> pd.DataFrame:
    """Extract player data from the market table view"""
    print("\nExtracting market players...")
    
    # Navigate to market
    page.goto("https://biwenger.as.com/market")
    
    # Switch to table view
    try:
        page.get_by_role("button", name=TABLE_VIEW_LABEL).click(timeout=3000)
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
            # Extract player data
            pos_locator = row.locator("player-position")
            pos_count = pos_locator.count()
            titles = [pos_locator.nth(j).get_attribute("title").strip() 
                     for j in range(pos_count)]
            position = "/".join(titles)

            club = safe_get_attribute(row.locator("a.team"), "title", "Unknown Club")
            name = safe_inner_text(row.locator("th a"), "Unknown Player")

            # Points data
            pts_cell = row.locator("td").nth(2)
            this_season_pts = safe_inner_text(pts_cell.locator(":scope").first, "0").split('\n')[0]
            last_season_pts = safe_inner_text(pts_cell.locator("div"), "0")
            
            # Market specific columns
            price_cell = row.locator("td").nth(3)  # Price column
            price = safe_inner_text(price_cell, "0").replace("€", "").strip()
            
            # Price change vs yesterday. Same <increment> pattern as
            # extract_value_and_delta() — sign comes from the CSS class
            # (increment/decrement), not the label text, since the "−"
            # character Biwenger uses isn't a plain ASCII hyphen.
            change_cell = row.locator("td").nth(4)
            change_label = change_cell.evaluate(
                "td => td.querySelector('increment')?.getAttribute('aria-label') || null"
            )
            change_cls = change_cell.evaluate(
                "td => td.querySelector('increment')?.className || ''"
            )
            change = parse_money(change_label) if change_label else 0.0
            if "decrement" in change_cls:
                change = -change

            status_cell = row.locator("td").nth(5)  # Status (Fit/Injured/Doubtful)
            status = safe_inner_text(status_cell, "Fit")

            # Was mislabeled "demand" — this column's title attribute is
            # literally "Points from the last rounds" (recent form), not
            # bid/interest count. Checked the market table headers and a
            # player's own detail page directly: Biwenger doesn't expose
            # live bid/demand data anywhere in the UI.
            recent_pts_cell = row.locator("td").nth(6)
            recent_pts = safe_inner_text(recent_pts_cell, "0")

            owner_cell = row.locator("td").nth(7)  # Owner column
            owner = safe_inner_text(owner_cell, "Free Agent")
            if owner.lower() != "free agent":
                print(f"Skipping player {name} owned by {owner}")
                continue

            sale_price_cell = row.locator("td").nth(8)  # Last sale price
            sale_price = safe_inner_text(sale_price_cell, "0").replace("€", "").strip()

            all_rows.append({
                "position": position,
                "club": club,
                "name": name,
                "price": price,
                "change": change,
                "status": status,
                "owner": owner,
                "last_sale": sale_price,
                "recent_pts": recent_pts,
                "this_season_pts": this_season_pts,
                "last_season_pts": last_season_pts,
                "scraped_at": pd.Timestamp.now().strftime("%Y-%m-%d")
            })

        except Exception as e:
            print(f"⚠️ Error processing market row: {e}")
            continue

    df = pd.DataFrame(all_rows)

    # Clean numerical columns (raw scraped strings, e.g. "€7,690,000")
    numeric_cols = ["price", "recent_pts", "this_season_pts", "last_season_pts"]
    for col in numeric_cols:
        cleaned = df[col].astype(str).str.replace(r"[^\d\.]", "", regex=True)
        df.loc[:, col] = pd.to_numeric(cleaned, errors="coerce").fillna(0)

    # 'change' is already a signed float from parse_money() — stripping
    # non-digit chars here would silently drop the minus sign.
    df.loc[:, "change"] = pd.to_numeric(df["change"], errors="coerce").fillna(0)

    filename = "csvs/market/market_players.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Saved {len(df)} market players to {filename}")
    return df

import time
import json
import json, time
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

def get_all_posts(page, max_scrolls=300, initial_wait=3, load_timeout_ms=6000,
                   stale_limit=3, checkpoint_every=5, stop_when_contains="Inici de joc"):
    """Scroll the league forum feed and collect every post.

    Three things that used to make this run forever:
    - it re-read every post on the page on every scroll (including ones
      already collected), so each pass got slower as the feed grew —
      quadratic in the number of posts.
    - the only stopping condition besides a fixed iteration count was "no
      new posts in a while" — but this league's board goes back years, so
      that condition basically never fires within a sane number of scrolls.
    - there was no way to bound the scrape to just the current season.

    Now it only extracts text for posts not already seen, checkpoints to
    disk periodically so an interruption doesn't lose everything, and
    stops as soon as it hits a post containing `stop_when_contains` — by
    default "Inici de joc" ("start of game"), Biwenger's own marker for
    the admin post that credits every team's starting budget at the top
    of a new season. That post is kept (need it for the money ledger);
    nothing older than it is fetched.
    """
    # Resume support: if we already have posts from a previous run, don't
    # re-scrape them — stop as soon as we catch up to already-known content.
    output_path = "unique_posts.json"
    existing_posts = []
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                existing_posts = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing_posts = []
    existing_ids = {json.dumps(p, ensure_ascii=False) for p in existing_posts}
    if existing_ids:
        print(f"Resuming: {len(existing_ids)} posts already saved from a "
              f"previous run, will stop once caught up to them.")

    new_posts = []
    seen = set()
    processed_count = 0  # DOM index of the last post we've already read

    def save(posts):
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(posts + existing_posts, f, ensure_ascii=False, indent=2)

    print(f"⏳ Waiting {initial_wait}s for first posts to render...")
    time.sleep(initial_wait)

    # Cookie banner (best-effort)
    try:
        page.get_by_role("button", name="Agree").click(timeout=3000)
    except:
        try:
            page.click("button#didomi-notice-agree-button", timeout=1000)
        except:
            pass

    post_locator = page.locator("league-board-post")

    # Ensure at least one post exists (don’t fail if none yet)
    try:
        page.wait_for_selector("league-board-post", timeout=5000)
    except PlaywrightTimeoutError:
        print("⚠️ No posts found at start.")
        return existing_posts

    print(f"Start: {post_locator.count()} posts")

    stale_scrolls = 0
    caught_up_scrolls = 0
    for i in range(max_scrolls):
        count_now = post_locator.count()

        new_this_round = 0
        already_known_this_round = 0
        hit_stop_marker = False
        for idx in range(processed_count, count_now):
            title = post_locator.nth(idx)
            post_data = title.inner_text().split("\n")
            post_id = json.dumps(post_data, ensure_ascii=False)
            if post_id in existing_ids:
                already_known_this_round += 1
                continue
            if post_id not in seen:
                seen.add(post_id)
                new_posts.append(post_data)
                new_this_round += 1
                if stop_when_contains and any(stop_when_contains in str(x) for x in post_data):
                    hit_stop_marker = True
                    break
        processed_count = count_now

        print(f"Scroll {i+1}: {count_now} posts on page, "
              f"{new_this_round} new, {already_known_this_round} already "
              f"saved, {len(new_posts)} new total")

        if hit_stop_marker:
            save(new_posts)
            print(f"Found '{stop_when_contains}' marker — that's the season "
                  f"start, stopping here ({len(new_posts)} new posts).")
            break

        if (i + 1) % checkpoint_every == 0:
            save(new_posts)
            print(f"  ↳ checkpoint saved ({len(new_posts)} new posts)")

        if existing_ids and new_this_round == 0 and already_known_this_round > 0:
            caught_up_scrolls += 1
            if caught_up_scrolls >= stale_limit:
                print(f"Caught up to previously saved posts — stopping "
                      f"({len(new_posts)} new posts found).")
                break
        else:
            caught_up_scrolls = 0

        if new_this_round == 0 and already_known_this_round == 0:
            stale_scrolls += 1
            if stale_scrolls >= stale_limit:
                print(f"No new posts for {stale_limit} scrolls in a row — "
                      f"reached the end of the feed, stopping early.")
                break
        else:
            stale_scrolls = 0

        # Scroll down to trigger loading more
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)  # give posts time to load

    combined = new_posts + existing_posts
    save(new_posts)
    print(f"✅ Saved {len(combined)} total posts to {output_path} "
          f"({len(new_posts)} new, {len(existing_posts)} carried over)")
    return combined

# Usage in your run() function:
def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(
        locale="ca-ES",
        extra_http_headers={"Accept-Language": "ca-ES,ca;q=0.9"},
        user_agent=DESKTOP_CHROME_UA,
    )
    page = context.new_page()

    # Login
    login(page)

    # Get all post titles (feeds the money ledger). max_scrolls is just a
    # safety cap now — it stops early once it stops finding new posts.
    get_all_posts(page)

    # Get league standings + rival team list
    rival_teams, links = get_league_standings(page)
    print(f"\nFound {len(rival_teams)} rival teams:")
    for team in rival_teams:
        print(f"{team['position']} - {team['name']} ({team['points']} pts)")

    # Extract players for each rival team
    for team in rival_teams:
        print(f"\nProcessing team: {team['name']} at position {team['position']}")
        extract_team_players(page, team["name"])
        # Go back to league view
        page.goto('https://biwenger.as.com/league')

    # Extract market players
    market_players_df = extract_market_players(page)
    print(f"Extracted {len(market_players_df)} market players → market_players.csv")

    # My own cash balance, scraped from /team — ground truth used to
    # sanity-check the forum-post-derived ledger balance in migration.py.
    my_balance = get_my_team_balance(page)
    if my_balance:
        pd.DataFrame([my_balance]).to_csv("csvs/others/my_balance.csv", index=False)

    # NOTE: extract_all_players() (full player database, not just market/rosters)
    # is intentionally left disabled here — nothing downstream consumes it yet
    # (migration.py has no players-table loader). Wiring that up is Day 1 work
    # (the deals engine), not part of getting the pipeline running again.

    context.close()
    browser.close()




def check_transfer_rumors(page, player_name, team_name):
    """Check for transfer rumors about a player"""
    try:
        # Navigate to the news section
        page.goto(f"https://www.futbolfantasy.com/laliga/equipos/{team_name.lower()}/mercado-fichajes/verano-2025")
        
        # Search for player name in news headlines
        news_items = page.locator(".news-item").all()
        rumors = []
        
        for item in news_items:
            headline = item.inner_text().lower()
            if player_name.lower() in headline:
                # Check for transfer-related keywords
                if any(word in headline for word in ["fichaje", "transfer", "oferta", "interés", "rumor"]):
                    rumors.append(headline.strip())
        
        if rumors:
            return " | ".join(rumors[:3])  # Return up to 3 rumors
        return "No recent rumors"
    except Exception as e:
        print(f"Error checking rumors for {player_name}: {e}")
        return "Error checking rumors"
    

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

def normalize_team_key(name):
    """Stable, accent/case-insensitive team identifier.

    Used as the CSV filename suffix and DB team_id so the same team never
    fragments into multiple records just because Biwenger rendered its name
    with different accents/casing on different scrapes.
    """
    key = normalize_player_name(name)
    key = re.sub(r"[^\w\s]", "", key)  # drop quotes/punctuation
    key = re.sub(r"\s+", "_", key.strip())
    return key

def scrape_player_probabilities(team):
    """Scrape player probabilities for a given team."""
    data = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        # Navigate to team page
        team_url = f"https://www.futbolfantasy.com/laliga/equipos/{team}"
        page.goto(team_url, timeout=30000)
        try:
            page.get_by_role("button", name="ACEPTO").click()
        except:
            pass
        page.get_by_role("link", name=" Lista").click()
        
        # Get player list
        tab = page.locator("div[class*='jugadores-titulares-'].mod.lesionados.mb-0")
        players = tab.locator("a.jugador.my-auto").all()
        
        for player in players:
            try:
                # Get player info
                href = player.get_attribute("href")
                name = href.split("/")[-1].replace("-", " ").title()
                
                # Navigate to player page
                page.goto(href, timeout=30000, wait_until="domcontentloaded")
                
                # Get probability
                page.wait_for_selector('span.mx-auto[class*="prob-"]', state="attached", timeout=10000)
                percentage = page.locator('span.mx-auto[class*="prob-"]').first.inner_text()
                
                # Store data
                data.append({
                    "Team": team.title(),
                    "Player": normalize_player_name(name),
                    "Probability": percentage
                })
                
                print(f"✅ {team} - {name}: {percentage}")
                
            except Exception as e:
                print(f"❌ Failed for {team} - {name}: {str(e)}")
            
            finally:
                # Return to team page
                page.go_back(wait_until="domcontentloaded")
                page.get_by_role("link", name=" Lista").click()
                tab = page.locator("div.jugadores-titulares-20078.mod.lesionados.mb-0")
        
        browser.close()
    
    return pd.DataFrame(data)

import re
import asyncio
import pandas as pd
from playwright.async_api import async_playwright

HEADLESS = True

async def scrape_player_probabilities_async(team: str, concurrency: int = 6) -> pd.DataFrame:
    """Scrape player probabilities for a given team (async concurrent version)."""
    def normalize_player_name(name: str) -> str:
        return name  # keep your existing implementation

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS, args=["--disable-dev-shm-usage"])
        context = await browser.new_context()

        async def route_handler(route, request):
            if request.resource_type in {"image", "media", "font", "stylesheet"} or any(
                s in request.url for s in ["googletagmanager", "google-analytics", "doubleclick", "facebook"]
            ):
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_handler)

        page = await context.new_page()
        team_url = f"https://www.futbolfantasy.com/laliga/equipos/{team}"
        await page.goto(team_url, timeout=30000, wait_until="domcontentloaded")

        try:
            await page.get_by_role("button", name=re.compile("ACEPTO|ACEPTAR|Aceptar", re.I)).click(timeout=2000)
        except Exception:
            pass

        try:
            await page.get_by_role("link", name=re.compile("Lista", re.I)).click(timeout=5000)
        except Exception:
            pass

        hrefs = await page.locator("div[class*='jugadores-titulares-'].mod.lesionados.mb-0 a.jugador.my-auto") \
                          .evaluate_all("els => els.map(e => e.href)")

        sem = asyncio.Semaphore(concurrency)
        results = []

        async def fetch(href: str):
            name = href.rstrip("/").split("/")[-1].replace("-", " ").title()
            async with sem:
                p = await context.new_page()
                try:
                    await p.goto(href, timeout=20000, wait_until="domcontentloaded")
                    await p.wait_for_selector('span.mx-auto[class*="prob-"]', state="attached", timeout=5000)
                    percentage = await p.locator('span.mx-auto[class*="prob-"]').first.text_content()
                    results.append({
                        "Team": team.title(),
                        "Player": normalize_player_name(name),
                        "Probability": (percentage or "").strip()
                    })
                    print(f"✅ {team} - {name}: {percentage}")
                except Exception as e:
                    print(f"❌ {team} - {name}: {e}")
                finally:
                    await p.close()

        await asyncio.gather(*(fetch(h) for h in hrefs))
        await browser.close()

    return pd.DataFrame(results)

# If you want a simple sync wrapper:
def scrape_player_probabilities_fast(team: str, concurrency: int = 6) -> pd.DataFrame:
    return asyncio.run(scrape_player_probabilities_async(team, concurrency=concurrency))

def get_starting_player_data():
    teams = [
        'alaves', 'athletic', 'atletico', 'barcelona', 'betis', 
        'celta', 'elche', 'espanyol', 'getafe', 'girona', 
        'levante', 'mallorca', 'osasuna', 'rayo-vallecano', 
        'real-madrid', 'real-oviedo', 'real-sociedad', 
        'sevilla', 'valencia', 'villarreal'
    ]
    
    all_data = pd.DataFrame()
    
    for team in teams:
        print(f"\n=== Scraping {team.title()} ===")
        team_df = scrape_player_probabilities_fast(team)
        all_data = pd.concat([all_data, team_df], ignore_index=True)
        
        # Save progress after each team
        all_data.to_csv("csvs/others/player_probabilities.csv", index=False)
        print(f"Saved data for {team.title()}")
    
    print("\n=== All teams scraped successfully ===")
    print(all_data)
    all_data.to_csv("csvs/others/player_probabilities.csv", index=False)

        
if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
    # get_starting_player_data()
