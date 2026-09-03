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

# Which of Biwenger's seven scoring systems this league plays under
# (5 = "AS.com and SofaScore average"). Every points figure the API
# returns is scoped to this — the same performance is worth a different
# number under each system — so it has to be passed on every call that
# reads points, and it has to match the league's own setting or the
# numbers silently disagree with the ones the site shows the user.
LEAGUE_SCORE_ID = 5

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

    The <balance> element's own inner text is ABBREVIATED for display
    ("-€3.8M"), with the exact figure sitting in its `title` attribute
    ("-€3,833,280") instead — confirmed live via the rendered DOM. Reading
    the inner text used to feed straight into parse_money(), which strips
    "." as a thousands separator (correct for Biwenger's normal
    "1.234.567 €" format elsewhere) — but here the "." in "3.8" is a
    decimal point, not a separator, so "-3.8" silently became "-38" and
    every downstream ledger-vs-actual comparison this season was checked
    against a balance off by roughly 100,000x. The title attribute is
    exact and uses the same period-as-thousands-separator format as
    everywhere else, so it needs no special-case parsing.
    """
    page.goto("https://biwenger.as.com/team", wait_until="domcontentloaded", timeout=15000)
    try:
        page.wait_for_selector("squad-stats balance", timeout=8000)
    except TimeoutError:
        print("⚠️ Could not find balance widget on /team")
        return {}

    balance_locator = page.locator("squad-stats balance")
    raw = balance_locator.get_attribute("title") or safe_inner_text(balance_locator, "")
    balance = parse_money(raw)

    manager_name = safe_inner_text(page.locator("a.avatar-container span, .user-name"), "")
    print(f"💶 My balance: €{balance:,.0f}")
    return {"balance": balance, "raw": raw, "scraped_at": pd.Timestamp.now().strftime("%Y-%m-%d")}

def get_la_liga_players() -> dict:
    """Fetch Biwenger's public La Liga player database (id -> full player
    record: name, price, teamID, position, ...). Public, no auth needed —
    the same cf.biwenger.com endpoint the app itself loads to render
    player cards. Used to resolve the numeric playerID references in
    /api/v2/user's market/offers/players arrays back to a name (and, for
    renewals, today's current price) we can act on.
    """
    import json
    import urllib.request

    url = f"https://cf.biwenger.com/api/v2/competitions/la-liga/data?lang=en&score={LEAGUE_SCORE_ID}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": DESKTOP_CHROME_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        players = data.get("data", {}).get("players", {})
        return {int(pid): p for pid, p in players.items()}
    except Exception as e:
        print(f"⚠️ Could not fetch la liga player database: {e}")
        return {}

def _capture_auth_headers(page, path="/team") -> dict:
    """Capture the auth headers (bearer token, league/user/version) the
    app's own HTTP client attaches to its API calls, by watching the
    real request the page fires when loading `path`. Reused by every
    function here that needs to call an authenticated Biwenger endpoint
    directly (offers, renewals) — replicating the app's own token/header
    logic from scratch would be far more fragile than just capturing it
    off a request the app makes itself.
    """
    captured = {}

    def _capture(req):
        if "api/v2/user?fields" in req.url and not captured:
            captured.update({
                k: v for k, v in req.headers.items()
                if k in ("authorization", "x-version", "x-lang", "x-user", "x-league", "accept", "accept-language", "content-type")
            })

    page.on("request", _capture)
    try:
        page.goto(f"https://biwenger.as.com{path}", wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(1000)
    finally:
        page.remove_listener("request", _capture)
    return captured

def get_my_offers(page) -> list:
    """Scrape purchase offers currently open on the logged-in user's
    players — what shows on biwenger.as.com/market/offers under
    "Purchase offers received for your players".

    /api/v2/user?fields=offers (tried first, historically) is a red
    herring — it's scoped to something else and was empty even with 14
    real offers live and visible on the actual offers page. The real
    data is /api/v2/market's `data.offers` array: one entry per squad
    player, `to` is the logged-in user, `from` is null (these are
    Biwenger's own algorithmic "instant sale to the Market" price, not a
    specific rival manager), `amount` is the offer, `requestedPlayers`
    is a one-element list with the numeric player id. Confirmed live —
    every amount matched the real page's numbers exactly (e.g. De la
    Fuente: listed €3,230,000, offer €3,380,200, a +€150,200 premium).

    Distinct from the market's own passive "on sale" listing (every
    owned player already has one of those, at roughly market value,
    already captured via team_players.price): this is what you'd
    actually be paid for an immediate guaranteed sale right now, which
    can be above OR below the listed price — the gap between the two is
    exactly the "is this offer worth taking" signal the sell recommender
    needs, not just whether an offer exists at all.
    """
    headers = _capture_auth_headers(page)
    if not headers:
        print("⚠️ Could not capture auth headers for offers request")
        return []

    try:
        resp = page.request.get(
            "https://biwenger.as.com/api/v2/market",
            headers=headers,
        )
        if resp.status != 200:
            print(f"⚠️ Offers request failed: HTTP {resp.status}")
            return []
        offers = resp.json().get("data", {}).get("offers", []) or []
        print(f"📨 {len(offers)} offer(s) on my players")
        return offers
    except Exception as e:
        print(f"⚠️ Could not fetch offers: {e}")
        return []

def renew_player_sales(page, la_liga_players=None) -> list:
    """Re-list every currently-owned player for sale at today's market
    value, keeping every listing continuously alive.

    Biwenger auto-lists every owned player for sale, but the listing
    expires after a fixed 48h window — confirmed live: the /team page
    shows "On sale for €X · tomorrow" (a rolling countdown) next to every
    squad player, and a real test renewal (with explicit user sign-off)
    returned "<Player> sale has been renewed." The UI's own Renew button
    simply re-POSTs the same {type, player, price} payload used to
    create the listing in the first place — confirmed by intercepting
    that exact network call. Running this on the same ~24h cadence as
    the rest of the daily scrape keeps every listing comfortably inside
    its 48h window, so nothing should ever lapse as long as the daily
    scrape keeps running.

    Renews at the CURRENT market price (from the public player
    database), not whatever price was last set — "auto-renew at market
    value" is what was asked for, not just resetting the timer on a
    possibly-stale price.
    """
    headers = _capture_auth_headers(page)
    if not headers:
        print("⚠️ Could not capture auth headers for renewal")
        return []

    try:
        resp = page.request.get(
            "https://biwenger.as.com/api/v2/user?fields=players(id,owner)",
            headers=headers,
        )
        if resp.status != 200:
            print(f"⚠️ Could not fetch owned player ids: HTTP {resp.status}")
            return []
        owned_ids = [p["id"] for p in resp.json().get("data", {}).get("players", []) or []]
    except Exception as e:
        print(f"⚠️ Could not fetch owned player ids: {e}")
        return []

    if la_liga_players is None:
        la_liga_players = get_la_liga_players()

    results = []
    for pid in owned_ids:
        info = la_liga_players.get(pid) or {}
        price = info.get("price")
        name = info.get("name") or str(pid)
        if not price:
            print(f"⚠️ Skipping renewal for player id {pid}: no current price found")
            results.append({"player_id": pid, "player_name": name, "price": None, "ok": False, "http_status": None})
            continue
        try:
            resp = page.request.post(
                "https://biwenger.as.com/api/v2/market",
                headers=headers,
                data=json.dumps({"type": "sell", "player": pid, "price": price}),
            )
            # Confirmed live: this endpoint returns 204 (No Content) on
            # success, not 200 — the UI's own success toast ("<player>
            # sale has been renewed.") fired for the exact same 204
            # response during testing, so treat any 2xx as success.
            ok = 200 <= resp.status < 300
            print(f"{'✅' if ok else '⚠️'} Renewed {name} @ €{price:,.0f} (HTTP {resp.status})")
            results.append({"player_id": pid, "player_name": name, "price": price, "ok": ok, "http_status": resp.status})
        except Exception as e:
            print(f"⚠️ Renewal failed for {name}: {e}")
            results.append({"player_id": pid, "player_name": name, "price": price, "ok": False, "http_status": None, "error": str(e)})

    succeeded = sum(1 for r in results if r["ok"])
    print(f"🔄 Renewed {succeeded}/{len(results)} listings")
    return results

def _fetch_round(round_id, version) -> dict:
    """One round's fixtures and per-player match reports, from the public
    cf.biwenger.com round endpoint. `round_id=None` asks for the current
    round, whose payload also carries the full season round list.

    `v` looks like a cache-buster but is not: it selects the scoring
    engine build, and asking for anything other than the app's own
    version silently returns DIFFERENT point totals for the same
    finished fixtures — nothing in the response says which build
    answered. Confirmed live on round 1: v=631 (the app's x-version at
    the time) gave Djené 2 / De la Fuente 4, matching the site exactly,
    while omitting `v` gave 4 / 7 and v=633 gave 5 / 0. So `version` is
    a required argument here, deliberately not defaulted — it must come
    from headers the app itself just sent.
    """
    import urllib.request

    path = f"/{round_id}" if round_id is not None else ""
    url = (f"https://cf.biwenger.com/api/v2/rounds/la-liga{path}"
           f"?score={LEAGUE_SCORE_ID}&lang=en&v={version}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": DESKTOP_CHROME_UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp).get("data", {}) or {}
    except Exception as e:
        print(f"⚠️ Could not fetch round {round_id}: {e}")
        return {}

def _fetch_round_ownership(page, headers, round_id) -> dict:
    """Who owned (and how they fielded) each player for a given round:
    player id -> {team_id, team, lineup_slot}.

    Read from the authenticated /api/v2/rounds/league/{roundId}, whose
    standings carry each manager's locked lineup for that round. Taken
    per round rather than from today's squads on purpose: ownership and
    the starter/bench call are what they were *at the time*, which is
    the only version of them that makes sense next to that round's
    scores.
    """
    try:
        resp = page.request.get(
            f"https://biwenger.as.com/api/v2/rounds/league/{round_id}",
            headers=headers,
        )
        if resp.status != 200:
            print(f"⚠️ Round {round_id} lineups failed: HTTP {resp.status}")
            return {}
        standings = resp.json().get("data", {}).get("league", {}).get("standings", []) or []
    except Exception as e:
        print(f"⚠️ Could not fetch round {round_id} lineups: {e}")
        return {}

    owners = {}
    for team in standings:
        team_name = team.get("name") or ""
        lineup = team.get("lineup") or {}
        # The coach is picked like a player and scores like one, but sits in
        # his own single-id slot rather than in any of the id lists.
        coach = (lineup.get("coach") or {}).get("id")
        slots = [("coach", [coach])] if coach else []
        slots += [(slot, lineup.get(key) or []) for slot, key in
                  (("starter", "players"), ("reserve", "reserves"),
                   ("discarded", "discarded"))]
        for slot, player_ids in slots:
            # `reserves` is a fixed-length list with null holes for empty
            # bench slots, so nulls have to be skipped rather than assumed absent.
            for player_id in player_ids:
                if player_id is None:
                    continue
                owners[player_id] = {
                    "team_id": normalize_team_key(team_name),
                    "team": team_name,
                    "lineup_slot": slot,
                }
    return owners

def _report_points(entry):
    """The score the site actually shows for one match report.

    Not simply `points`: this competition runs "Super Pica" on scoring
    systems 1, 5 and 8 (competition config `superPicaScores`, which
    LEAGUE_SCORE_ID is one of), and when a performance qualifies, the
    API returns the plain total in `points` AND the one the site
    displays in `optionalPoints.superPicaExtraPoints.points`. Reading
    `points` alone quietly undercounts exactly the standout games that
    matter most — caught against the live match pages: Mariano 16 vs the
    17 shown, Roberto Fernández 17 vs 19, Kang-in Lee 14 vs 16.

    The field is absent whenever it doesn't apply, so its presence is
    the whole test — `star`/`profitable` are not: Tenaglia was flagged
    star with no Super Pica bonus and the site showed his base 16.
    """
    optional = entry.get("optionalPoints") or {}
    super_pica = (optional.get("superPicaExtraPoints") or {}).get("points")
    return super_pica if super_pica is not None else entry.get("points")

def _round_score_rows(round_data, owners, la_liga_players) -> list:
    """Flatten one round's payload into one row per (fixture side, player).

    played/DNP is decided from each FIXTURE's own status, never the
    round's. A round stays "active" while some of its games are still
    unplayed, so a missing score means two completely different things
    depending on the game it belongs to: "kick-off hasn't happened yet"
    (game still pending/preview — what the site renders as "?") versus
    "the game finished without him" (game finished, no match report —
    what the site renders as "-"). Both store points=None; `played` and
    `game_status` are what separate them.

    Coaches are scored too in this league, but they never appear in
    `reports` — they hang off the fixture side's own `coach` key — so
    they're folded in here rather than silently dropped as DNP.
    """
    rows = []
    round_id = round_data.get("id")
    round_name = round_data.get("name")
    round_status = round_data.get("status")

    for game in round_data.get("games") or []:
        game_id = game.get("id")
        game_status = game.get("status")
        for side in ("home", "away"):
            club = game.get(side) or {}
            club_id = club.get("id")
            club_name = club.get("name")

            entries = list(club.get("reports") or [])
            coach = club.get("coach")
            if coach:
                entries.append(coach)

            def _row(player_id, name, position, points):
                own = owners.get(player_id) or {}
                return {
                    "round_id": round_id,
                    "round_name": round_name,
                    "round_status": round_status,
                    "game_id": game_id,
                    "game_status": game_status,
                    "player_id": player_id,
                    "player": name,
                    "club": club_name,
                    "position": position,
                    "team_id": own.get("team_id"),
                    "team": own.get("team"),
                    "lineup_slot": own.get("lineup_slot"),
                    "points": points,
                    "played": int(points is not None and game_status == "finished"),
                }

            reported = set()
            for entry in entries:
                player = entry.get("player") or {}
                player_id = player.get("id")
                if player_id is None:
                    continue
                reported.add(player_id)
                rows.append(_row(player_id, player.get("name"),
                                 player.get("position"), _report_points(entry)))

            # Anyone owned in this league whose club played this fixture but
            # who has no report at all — the DNP case. Only owned players get
            # these filler rows: a row per unreported La Liga player would
            # multiply the table's size for data nothing downstream asks for.
            for player_id, own in owners.items():
                if player_id in reported:
                    continue
                info = la_liga_players.get(player_id) or {}
                if info.get("teamID") != club_id:
                    continue
                rows.append(_row(player_id, info.get("name"),
                                 info.get("position"), None))

    return rows

def get_round_scores(page, la_liga_players=None) -> list:
    """Every player's score, round by round, for every round that has at
    least one finished fixture — the per-matchday history the DB
    otherwise only had season aggregates for.

    Rounds are taken from the season list rather than a range of ids
    because Biwenger's ids aren't in calendar order (round 1 is 4899 but
    its postponed sibling is 4937, sitting between rounds 2 and 3), and
    they're filtered on "has a finished game" rather than on the round's
    own `status` because that status is unreliable in both directions:
    the round holding round 1's finished games is still "active", and
    the duplicate "(postponed)" round is "pending" despite listing those
    same finished games.

    Those "(postponed)" rounds are skipped outright (`part` > 1). They
    exist so managers can field a second lineup for the fixtures that
    slipped, but they re-list the *same* game ids with the same reports,
    so ingesting them would file every score in the round twice.
    """
    headers = _capture_auth_headers(page)
    version = headers.get("x-version") if headers else None
    if not version:
        # Guessing a version is worse than returning nothing: see
        # _fetch_round for why a wrong `v` yields wrong numbers quietly.
        print("⚠️ No x-version in captured headers — skipping round scores")
        return []

    if la_liga_players is None:
        la_liga_players = get_la_liga_players()

    season = _fetch_round(None, version).get("season") or {}
    rounds = season.get("rounds") or []
    if not rounds:
        print("⚠️ Could not list the season's rounds — skipping round scores")
        return []

    rows = []
    for entry in rounds:
        round_id = entry.get("id")
        round_data = _fetch_round(round_id, version)
        if not round_data or (round_data.get("part") or 1) != 1:
            continue
        games = round_data.get("games") or []
        if not any(g.get("status") == "finished" for g in games):
            continue

        owners = _fetch_round_ownership(page, headers, round_id)
        round_rows = _round_score_rows(round_data, owners, la_liga_players)
        rows.extend(round_rows)
        finished = sum(1 for g in games if g.get("status") == "finished")
        print(f"📅 {round_data.get('name')}: {len(round_rows)} player rows "
              f"({finished}/{len(games)} fixtures finished)")

    print(f"🗓️ {len(rows)} round-score rows across "
          f"{len({r['round_id'] for r in rows})} round(s)")
    return rows

# Biwenger encodes a player's position as a small int everywhere in its
# own APIs (player database, lineups). Confirmed by cross-checking each
# rival's locked lineup against its declared formation: a "4-4-2" team's
# `lineup.players` array resolves to positions [1,2,2,2,2,3,3,3,3,4,4]
# and a "3-4-3" to [1,2,2,2,3,3,3,3,4,4,4] — GK first, then defenders,
# midfielders, forwards, in formation order, every time.
BIWENGER_POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

def _board_txn_key(player_id, winner_id, amount) -> str:
    """Stable identity for one completed market transaction, used to
    append only genuinely-new rows to market_bid_history across runs.

    Deliberately excludes the board entry's own `date` timestamp, for the
    same reason _post_identity drops the forum post's date (see its
    docstring, and the double-counted-ledger bug that motivated it): a
    timestamp is the one field a feed can legitimately re-render, and any
    such re-rendering silently turns one real transaction into two. The
    content triple (which player, which buyer, exact euro amount) is
    already a near-unique key on its own — two genuinely distinct sales
    of the same player to the same manager for the exact same amount to
    the euro is far less likely than another unnoticed timestamp quirk.
    """
    return f"{player_id}:{winner_id}:{int(amount)}"

def get_league_board_market(page, la_liga_players=None) -> list:
    """Capture every completed market transaction currently visible in the
    league activity feed, with the FULL list of competing bids — who bid
    and exactly how much, not just how many people bid.

    Source is /api/v2/home's `data.league.board`: a mixed activity feed
    (transfer / playerMovements / salaries / market entries). The `market`
    entries carry `content`, a list of one dict per completed signing:
    `{"player": <id>, "to": {"id","name"}, "amount": <winning bid>,
      "bids": [{"user": {"id","name"}, "amount": <losing bid>}, ...]}`.
    Confirmed live: `bids` holds the LOSING bids only — the winner appears
    solely as `to`/`amount` and never inside `bids` — so the real number of
    bidders is len(bids) + 1.

    This is a strict upgrade over the bid_history table built from the
    forum ledger, which only ever recovers a bid *count* per sale. But it
    comes with a hard constraint: the board is a fixed, shallow rolling
    window (8 entries total, all types combined, confirmed live) with no
    paging — ?offset/?limit/?type are all accepted and all ignored, and
    /api/v2/board 400s. There is no way to backfill it. So this has to run
    on every daily scrape and accumulate, and the useful sample size grows
    one day at a time.

    Each bid gets its own row (the winner too, flagged is_winner=1) so a
    consumer can look at the whole auction — the gap to the runner-up is
    what says whether the winner overpaid or just barely cleared the pack.

    player_price is today's market price from the public player database,
    recorded alongside because the bid amounts are meaningless without a
    price to compare them against, and that price is NOT recoverable
    later — it moves daily. It is an approximation of the price bidders
    actually saw: the board window can be a couple of days deep, and a
    just-sold player's price typically jumps on the sale, so price_change
    (the same day's own increment) is stored too, letting a consumer back
    out roughly what the pre-sale price was.
    """
    headers = _capture_auth_headers(page)
    if not headers:
        print("⚠️ Could not capture auth headers for league board request")
        return []

    try:
        resp = page.request.get("https://biwenger.as.com/api/v2/home", headers=headers)
        if resp.status != 200:
            print(f"⚠️ League board request failed: HTTP {resp.status}")
            return []
        board = resp.json().get("data", {}).get("league", {}).get("board", []) or []
    except Exception as e:
        print(f"⚠️ Could not fetch league board: {e}")
        return []

    if la_liga_players is None:
        la_liga_players = get_la_liga_players()

    scraped_at = pd.Timestamp.now().strftime("%Y-%m-%d")
    rows = []
    for entry in board:
        if entry.get("type") != "market":
            continue
        entry_date = entry.get("date")
        for txn in entry.get("content") or []:
            player_id = txn.get("player")
            winner = txn.get("to") or {}
            amount = txn.get("amount")
            if player_id is None or amount is None or not winner.get("id"):
                continue
            losing_bids = txn.get("bids") or []
            info = la_liga_players.get(player_id) or {}
            common = {
                "txn_key": _board_txn_key(player_id, winner["id"], amount),
                "txn_date": entry_date,
                "player_id": player_id,
                "player_name": info.get("name"),
                "player_position": BIWENGER_POSITIONS.get(info.get("position")),
                "player_price": info.get("price"),
                "price_change": info.get("priceIncrement"),
                "winner_id": winner["id"],
                "winner_name": winner.get("name"),
                "winning_amount": amount,
                # Total bidders in the auction, winner included — the
                # directly comparable number to bid_history.bids.
                "num_bidders": len(losing_bids) + 1,
                "scraped_at": scraped_at,
            }
            # bid_rank 0 is the winner, then the losing bids from highest
            # down (the feed already returns them descending; sorted here
            # anyway rather than trusting that ordering).
            ranked = [(winner["id"], winner.get("name"), amount, 1)]
            for b in sorted(losing_bids, key=lambda x: x.get("amount") or 0, reverse=True):
                user = b.get("user") or {}
                ranked.append((user.get("id"), user.get("name"), b.get("amount"), 0))
            for rank, (bidder_id, bidder_name, bid_amount, is_winner) in enumerate(ranked):
                rows.append({**common, "bidder_id": bidder_id, "bidder_name": bidder_name,
                             "bid_amount": bid_amount, "is_winner": is_winner, "bid_rank": rank})

    txns = len({r["txn_key"] for r in rows})
    print(f"🧾 Captured {txns} completed market transaction(s), {len(rows)} individual bid(s) from the league board")
    return rows

def get_rival_lineups(page, la_liga_players=None) -> list:
    """Capture every team's locked-in lineup for the upcoming round.

    /api/v2/rounds/league's `data.league.standings` gives, per team, its
    Biwenger user id, name, points, teamValue/teamValueInc, position, and
    a `lineup` block: the chosen formation (`type`, e.g. "4-4-2"), the
    captain, and three disjoint player-id lists — `players` (the starting
    XI, ordered to match the formation), `reserves` (the bench, which can
    contain nulls for unfilled slots) and `discarded`.

    Nothing else scraped here has this: team_players is a flat roster with
    no notion of who its owner is actually fielding. That distinction is
    what makes a competitor model possible — a team starting four
    defenders out of a five-defender squad has no room for another one,
    while a team fielding a defender it also has on the bench-and-discard
    pile is a real bidder for an upgrade.

    One row per player per team per run, tagged with the slot it occupies.
    Positions are resolved through the public player database rather than
    inferred from the array index, so this doesn't silently break if
    Biwenger ever stops ordering `players` by formation.
    """
    headers = _capture_auth_headers(page)
    if not headers:
        print("⚠️ Could not capture auth headers for lineups request")
        return []

    try:
        resp = page.request.get("https://biwenger.as.com/api/v2/rounds/league", headers=headers)
        if resp.status != 200:
            print(f"⚠️ Lineups request failed: HTTP {resp.status}")
            return []
        data = resp.json().get("data", {})
    except Exception as e:
        print(f"⚠️ Could not fetch rival lineups: {e}")
        return []

    standings = (data.get("league") or {}).get("standings") or []
    round_id = (data.get("round") or {}).get("id")

    if la_liga_players is None:
        la_liga_players = get_la_liga_players()

    scraped_at = pd.Timestamp.now().strftime("%Y-%m-%d")
    rows = []
    for team in standings:
        lineup = team.get("lineup") or {}
        captain = (lineup.get("captain") or {}).get("id")
        base = {
            "round_id": round_id,
            "user_id": team.get("id"),
            "team_name": team.get("name"),
            # normalize_team_key so this joins to the team_id every other
            # table (team_players, team_balance) is keyed on.
            "team_id": normalize_team_key(team.get("name") or ""),
            "league_position": team.get("position"),
            "points": team.get("points"),
            "team_value": team.get("teamValue"),
            "team_value_inc": team.get("teamValueInc"),
            "formation": lineup.get("type"),
            "scraped_at": scraped_at,
        }
        for slot, ids in (("starter", lineup.get("players")),
                          ("reserve", lineup.get("reserves")),
                          ("discarded", lineup.get("discarded"))):
            for pid in ids or []:
                if pid is None:  # unfilled bench slot
                    continue
                info = la_liga_players.get(pid) or {}
                rows.append({**base, "slot": slot, "player_id": pid,
                             "player_name": info.get("name"),
                             "position": BIWENGER_POSITIONS.get(info.get("position")),
                             "price": info.get("price"),
                             "is_captain": int(pid == captain)})

    print(f"📋 Captured lineups for {len(standings)} team(s) ({len(rows)} player slots) in round {round_id}")
    return rows

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
                "club": normalize_player_name(club),
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

# Matches every date-like token Biwenger's forum feed renders, both
# while a post is recent (relative — "Ara mateix"/"just now", "Fa 2
# hores"/"2 hours ago", "Ahir"/"yesterday") AND once it's aged into a
# fixed Catalan date ("15 d'ag.", "8 de set. 2025"). A single post's
# own timestamp text changes across ALL of these forms over its life —
# confirmed live: the identical "Fitxat per" block first seen as
# "Fa 3 hores" was seen again, unchanged otherwise, as "Ahir" the next
# day. Stripping only the relative forms (an earlier version of this
# fix) caught same-day re-scrapes but not the eventual settle into an
# absolute date, which itself then read as a "new" post — verified
# against all ~500 distinct date-like tokens actually present in
# unique_posts.json to confirm this doesn't also match real content
# (player names, amounts, team names never matched).
# Known manager renames — confirmed via league_standings.csv history that
# the two names never coexist as separate teams across the whole season,
# only ever one or the other (same team_id, same roster size/value
# trajectory, just a different display name after some date). Map old
# name -> current canonical name; add a new entry here if another rename
# is ever caught the same way (a real transaction duplicate-counted
# because a team's name differs between two scrapes of the same post).
TEAM_NAME_ALIASES = {
    'FerranGoaT': 'FreeJulian',
}

_DATE_TOKEN_RE = re.compile(
    r"^(Ara mateix|Avui|Ahir|Demà"
    r"|Fa \d+ \S+"
    r"|\d{1,2} d['’]?e?\.?\s?\S+\.?(\s\d{4})?)$"
)
# A bare integer, standing alone with no unit/currency/word attached, is a
# reaction/like count on that post (or on each item within a batch post) —
# never real transaction content, which is always tagged ("X licitacions",
# "€X", a name, a position code). Confirmed at scale across the real
# dataset: every one of 670 bare-integer tokens found immediately follows a
# "Venut per X€" / "Per X€" / "X licitacions" token, never appears as
# content on its own. Like a date string, this field re-renders as people
# react to a post after it's first scraped, so hashing it double/triple-
# counted real sales exactly like the timestamp did (caught live: the same
# real Bartra sale, scraped on two days with reaction counts '5' and '0',
# produced two different identities and was recorded as two separate
# sales in realized_trades). It can go negative too (a net score, not a
# raw count) — first regex version only matched bare positive digits and
# missed a real duplicate as a result: the same Mantilla/Hernan Krezzpo
# transfer, scraped once with a trailing '-2' and once with '0', produced
# two distinct identities and was double-counted in realized_trades.
_BARE_INT_RE = re.compile(r"^-?\d+$")

def _post_identity(post_data):
    """A stable identity for a scraped post, for deduplication across
    scrapes. Hashing the whole post text seems safe but isn't: the same
    real post's timestamp text changes every time it's re-scraped until
    it finally settles (see _DATE_TOKEN_RE), and a trailing reaction
    count changes too as people react to a post after it's first seen
    (see _BARE_INT_RE) — either was silently treated as turning one real
    post into a new one. This double/triple-counted real transactions in
    the money ledger, confirmed live against multiple teams' balances not
    matching reality. Both are dropped from the identity entirely rather
    than partially: the transaction content (type, team, player, exact
    euro amount) is already a near-unique key on its own, so the risk of
    two genuinely different transactions colliding is far smaller than
    the risk of missing another still-unrecognized volatile field down
    the line.

    A THIRD volatile field, of a completely different kind: a manager's
    team NAME itself can change mid-season (a rename), and Biwenger
    re-renders old posts with whatever name is current at scrape time —
    confirmed live, two "Inici de joc" (season-start budget credit)
    posts were byte-identical except one read "FreeJulian" where the
    other read "FerranGoaT", the same team before/after a rename. That's
    not a token this function can generically recognize and strip (it's
    an arbitrary string, not a date or a number) — but there is exactly
    one Inici de joc event per season ever, by construction, so any post
    containing it collapses to one constant identity regardless of which
    team names happen to appear in it, rather than trying to strip just
    the renamed token.
    """
    if any(isinstance(x, str) and x.strip() == 'Inici de joc' for x in post_data):
        return '"__SEASON_START_CREDIT__"'
    stable = [
        x for x in post_data
        if not (isinstance(x, str) and (_DATE_TOKEN_RE.match(x.strip()) or _BARE_INT_RE.match(x.strip())))
    ]
    # The Inici de joc case above is one symptom of a bigger issue: a
    # rename can hit ANY post, not just that one, and a batch post
    # ("MERCAT DE FITXATGES" etc.) bundles many teams' real transactions
    # together — a rename on ONE of them makes the whole post's text
    # differ, duplicate-counting every OTHER real transaction bundled
    # alongside it too, including ones that have nothing to do with the
    # renamed team. Confirmed via league_standings.csv history: FerranGoaT
    # and FreeJulian never coexist as two teams, only ever one or the
    # other — the same manager, renamed mid-season. Normalizing known
    # aliases to one canonical name before hashing fixes every post this
    # touches at once, not just the one that happened to be caught live.
    for old_name, canonical in TEAM_NAME_ALIASES.items():
        stable = [x.replace(old_name, canonical) if isinstance(x, str) else x for x in stable]
    return json.dumps(stable, ensure_ascii=False)

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
    existing_ids = {_post_identity(p) for p in existing_posts}
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
            post_id = _post_identity(post_data)
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

    # Pending purchase offers on my players, plus the public player
    # database needed both to resolve offers' numeric playerID -> name
    # and (below) to price today's renewals.
    offers = get_my_offers(page)
    la_liga_players = get_la_liga_players()

    def _offer_player(o):
        """requestedPlayers[0] is a bare numeric id for Biwenger's own
        algorithmic "instant sale" offers (from: null), which is all this
        was ever built against — see get_my_offers' docstring. A live
        manager-to-manager offer (like the direct offers this bot can
        place) embeds a full player object there instead, which broke the
        bare-id assumption with `TypeError: unhashable type: 'dict'` from
        la_liga_players.get(<dict>). Handle both shapes, and use the
        embedded name directly when there is one instead of a second
        la_liga_players lookup.
        """
        players = o.get("requestedPlayers") or []
        if not players:
            return None, None
        p = players[0]
        if isinstance(p, dict):
            return p.get("id"), p.get("name")
        return p, None

    offer_rows = []
    for o in offers:
        player_id, embedded_name = _offer_player(o)
        offer_rows.append({
            "player_id": player_id,
            "player_name": embedded_name or (la_liga_players.get(player_id) or {}).get("name"),
            "price": o.get("amount"),
            "date": o.get("created"),
            "until": o.get("until"),
            "raw_json": json.dumps(o),
            "scraped_at": pd.Timestamp.now().strftime("%Y-%m-%d"),
        })
    offer_columns = ["player_id", "player_name", "price", "date", "until", "raw_json", "scraped_at"]
    pd.DataFrame(offer_rows, columns=offer_columns).to_csv("csvs/others/my_offers.csv", index=False)
    print(f"Saved {len(offer_rows)} pending offers → my_offers.csv")

    # Per-round, per-player scores. Written with an explicit column list so
    # the CSV still has a usable header on a season's very first run, when
    # no round has finished yet and there are no rows to infer one from.
    round_score_columns = [
        "round_id", "round_name", "round_status", "game_id", "game_status",
        "player_id", "player", "club", "position", "team_id", "team",
        "lineup_slot", "points", "played",
    ]
    round_scores = get_round_scores(page, la_liga_players)
    pd.DataFrame(round_scores, columns=round_score_columns).to_csv(
        "csvs/others/round_scores.csv", index=False)
    print(f"Saved {len(round_scores)} round scores → round_scores.csv")

    # Real bid competition: who actually bid on recently-sold players and
    # exactly how much, plus every rival's locked lineup for the upcoming
    # round. Both feed recommenders.py's buy-price model. The board is a
    # shallow rolling window with no paging (see get_league_board_market),
    # so this only ever accumulates by running daily — migration.py
    # appends just the transactions it hasn't already stored.
    board_rows = get_league_board_market(page, la_liga_players)
    board_columns = ["txn_key", "txn_date", "player_id", "player_name", "player_position",
                     "player_price", "price_change", "winner_id", "winner_name",
                     "winning_amount", "num_bidders", "bidder_id", "bidder_name",
                     "bid_amount", "is_winner", "bid_rank", "scraped_at"]
    pd.DataFrame(board_rows, columns=board_columns).to_csv("csvs/others/market_bids.csv", index=False)
    print(f"Saved {len(board_rows)} market bids → market_bids.csv")

    lineup_rows = get_rival_lineups(page, la_liga_players)
    lineup_columns = ["round_id", "user_id", "team_name", "team_id", "league_position",
                      "points", "team_value", "team_value_inc", "formation", "slot",
                      "player_id", "player_name", "position", "price", "is_captain",
                      "scraped_at"]
    pd.DataFrame(lineup_rows, columns=lineup_columns).to_csv("csvs/others/rival_lineups.csv", index=False)
    print(f"Saved {len(lineup_rows)} lineup slots → rival_lineups.csv")

    # Keep every owned player's sale listing alive at current market
    # value (see renew_player_sales' docstring for why this is safe/
    # needed). Written to a JSON file rather than sent straight to
    # Telegram from here — notify.py folds this into the same daily
    # digest as the buy/sell recommendations, after migration.py has run,
    # so it's one message instead of two, and only notify.py needs the
    # Telegram secret.
    renewal_results = renew_player_sales(page, la_liga_players)
    with open("csvs/others/renewal_results.json", "w") as f:
        json.dump(renewal_results, f, indent=2)

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


def _accept_futbolfantasy_cookies(page):
    """Best-effort dismissal of futbolfantasy.com's Sirdata CMP consent banner.

    Only appears once per browser context (first navigation) — a no-op
    (caught below) on every team page after the first. The button's CSS
    class is build-generated (e.g. "sd-cmp-7Ga7b") and not stable across
    deploys, and its text language depends on the browser's effective
    Accept-Language: a fresh headless context with no locale configured
    (what this scraper uses) rendered it as "Accept all" (English) when
    checked live, not the "Aceptar todo" a Spanish-locale browser shows —
    match both, plus the old "ACEPTO" this code used to look for (never
    actually seen live against the current site, kept just in case some
    locale/AB-test still renders it).
    """
    try:
        page.get_by_role(
            "button", name=re.compile(r"Accept all|Aceptar todo|ACEPTO", re.I)
        ).click(timeout=5000)
    except Exception:
        pass


def _block_heavy_resources(context):
    """Abort images/fonts/media/stylesheets and known ad/tracker hosts.

    futbolfantasy.com team pages pull in a large amount of ad-tech (Twitch
    embeds, Amazon's ad system, Sirdata consent-sync pixels, Google ad
    scripts...) that has nothing to do with the lineup data this scraper
    reads. None of it is needed to see the `data-probabilidad` /
    `data-nombre` attributes we scrape (those are already present in the
    team page's own markup once the "Lista" tab renders, not fetched by
    any of these third parties) — cutting it out makes each page load
    faster and lighter on the renderer.
    """
    def route_handler(route, request):
        if request.resource_type in {"image", "media", "font", "stylesheet"} or any(
            s in request.url for s in (
                "googletagmanager", "google-analytics", "doubleclick",
                "facebook", "twitch", "amazon-adsystem", "sddan.com",
                "googlesyndication",
            )
        ):
            route.abort()
        else:
            route.continue_()
    context.route("**/*", route_handler)


def _scrape_team_probabilities(page, team: str) -> list:
    """Extract every squad player's start probability for `team`.

    Navigates to the futbolfantasy.com team page, switches its "Posible
    alineación" widget to the "Lista" tab, and reads the probability
    straight off each player row's `data-probabilidad` attribute — e.g.
    `<div class="jugador_7279 jugador tipo_lista block-new"
    data-nombre="joan-garcia" data-probabilidad="80%" ...>`. Confirmed live
    against Barcelona/Real Madrid/Atlético/Elche/Valencia/Villarreal/Betis
    team pages.

    This deliberately does NOT open each player's own page (that's what the
    retired `scrape_player_probabilities_async`/`_fast` used to do, up to
    20+ page navigations per team, 6 concurrent) — the same probability is
    already sitting in the Lista view's DOM, so there is nothing to gain
    from the extra navigations and they were almost certainly the source of
    the "Target crashed" failures reported when this was last debugged:
    that many concurrent/sequential page objects is a lot of renderer churn
    for headless Chromium, especially under a memory-constrained CI/sandbox.
    Reading the data already on the page sidesteps that failure mode
    entirely instead of retrying around it.

    Two selector traps to be aware of if this breaks again:
    - The "Lista" tab must be matched as `a.lista-tab`, not by role/name
      "Lista" text — the team page also has an unrelated news article
      link whose title happens to contain "Lista", which makes any
      name-text match ambiguous (Playwright strict-mode violation).
    - Not every team has this widget at all. Newly-promoted/less-covered
      teams (confirmed live: Real Oviedo, early in the 25/26 season) can
      have no "Posible alineación" section published yet — no Campo/Lista
      toggle exists on the page. That's a legitimate empty result, not a
      broken selector, so it's handled as a skip rather than an error.
    """
    team_url = f"https://www.futbolfantasy.com/laliga/equipos/{team}"
    page.goto(team_url, timeout=30000, wait_until="domcontentloaded")
    _accept_futbolfantasy_cookies(page)

    lista_tab = page.locator("a.lista-tab")
    if lista_tab.count() == 0:
        print(f"⚠️ {team}: no 'Posible alineación' widget on this team page — skipping")
        return []

    try:
        lista_tab.first.click(timeout=5000)
    except Exception as e:
        print(f"⚠️ {team}: could not click the Lista tab: {e}")
        return []

    # Before the click, the widget's default "Campo" (pitch) view already
    # has the container in the DOM with just the 11 starters (used to place
    # the pitch icons). Clicking "Lista" re-renders it to include the bench
    # too (rows carry an extra "isSuplente" class) — wait for that so we
    # don't read a stale, starters-only snapshot from before the click took
    # effect. Confirmed live this settles in well under a second.
    container_selector = "div[class*='jugadores-titulares-'].mod.lesionados.mb-0"
    try:
        page.wait_for_function(
            """(sel) => {
                const c = document.querySelector(sel);
                return !!(c && c.querySelector('.isSuplente'));
            }""",
            arg=container_selector,
            timeout=8000,
        )
    except Exception:
        # Very short benches could in principle never produce an
        # ".isSuplente" row — fall back to just "some rows are there".
        try:
            page.wait_for_selector(f"{container_selector} > div[data-nombre]", timeout=5000)
        except Exception as e:
            print(f"⚠️ {team}: player list never appeared: {e}")
            return []

    rows = page.locator(f"{container_selector} > div[data-nombre]")
    count = rows.count()

    data = []
    for i in range(count):
        row = rows.nth(i)
        try:
            name = row.locator("span.nombre").inner_text(timeout=2000).strip()
        except Exception:
            # Fall back to the URL-style slug if the name span is missing.
            slug = row.get_attribute("data-nombre") or ""
            name = slug.replace("-", " ").title()

        probability = (row.get_attribute("data-probabilidad") or "").strip()
        if not probability:
            continue

        data.append({
            "Team": team.replace("-", " ").title(),
            "Player": normalize_player_name(name),
            "Probability": probability,
        })
        print(f"✅ {team} - {name}: {probability}")

    return data


def scrape_player_probabilities(team: str) -> pd.DataFrame:
    """Scrape start probabilities for every squad player of `team`.

    Standalone entry point that launches and tears down its own browser —
    handy for testing a single team. `get_starting_player_data()` below
    reuses one browser/page across all teams instead, since (per
    `_scrape_team_probabilities`'s docstring) each team is now just a
    single page load, so there's no benefit to a fresh browser per team.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        _block_heavy_resources(context)
        page = context.new_page()
        try:
            data = _scrape_team_probabilities(page, team)
        finally:
            browser.close()
    return pd.DataFrame(data)


def get_starting_player_data():
    """Scrape start probabilities for every player on every La Liga team.

    Slow (~20 sequential external page loads) and deliberately NOT part of
    the daily `run()` pipeline — see the note above `if __name__ ==
    "__main__":` below for how to invoke it directly. If this ends up
    needing its own cadence (e.g. a couple of times a week, ahead of each
    matchday, since lineups firm up close to kickoff), that should be a
    separate scheduled workflow rather than folded into the daily Biwenger
    scrape — a slow or blocked futbolfantasy.com shouldn't be able to hold
    up the core pipeline. Out of scope here; not built.
    """
    # This list was missing 3 clubs that Biwenger itself actually has
    # players for this season (found by cross-checking DISTINCT club
    # against this list): Racing, Deportivo, Málaga. Verified each slug
    # resolves (200, not 404) on futbolfantasy.com before adding. Not
    # removing anything from the original 20 since there's no reliable way
    # to confirm which (if any) are stale without real-world knowledge of
    # this specific season's promotions/relegations — a wrong slug here
    # just gets skipped gracefully (see the "no widget" branch above), so
    # slight over-inclusion is the safe direction to err in.
    teams = [
        'alaves', 'athletic', 'atletico', 'barcelona', 'betis',
        'celta', 'elche', 'espanyol', 'getafe', 'girona',
        'levante', 'mallorca', 'osasuna', 'rayo-vallecano',
        'real-madrid', 'real-oviedo', 'real-sociedad',
        'sevilla', 'valencia', 'villarreal',
        'racing', 'deportivo', 'malaga',
    ]

    all_rows = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        _block_heavy_resources(context)
        page = context.new_page()

        for team in teams:
            print(f"\n=== Scraping {team.title()} ===")
            try:
                team_rows = _scrape_team_probabilities(page, team)
            except Exception as e:
                print(f"❌ {team}: unexpected error, skipping: {e}")
                team_rows = []

            all_rows.extend(team_rows)

            # Save progress after each team so a later failure doesn't lose
            # everything scraped so far.
            pd.DataFrame(all_rows).to_csv("csvs/others/player_probabilities.csv", index=False)
            print(f"Saved data for {team.title()} ({len(team_rows)} players, {len(all_rows)} total so far)")

        browser.close()

    df = pd.DataFrame(all_rows)
    print(f"\n=== All teams scraped: {len(df)} total player probabilities ===")
    df.to_csv("csvs/others/player_probabilities.csv", index=False)
    return df


if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
    # get_starting_player_data()
