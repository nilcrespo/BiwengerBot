import re
from matplotlib import table
import pandas as pd
from playwright.sync_api import Playwright, sync_playwright, expect, TimeoutError
from typing import List, Dict

# Configuration
import os

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

def safe_inner_text(locator, default="", timeout=1000):
    """Safely get inner text with timeout handling"""
    try:
        locator.first.wait_for(state="visible", timeout=timeout)
        txt = locator.first.inner_text(timeout=timeout)
        return txt.strip()
    except TimeoutError:
        return default

def extract_team_players(page, team_name: str) -> pd.DataFrame:
    """Extract player data for a specific team"""
    print(f"\nExtracting players for {team_name}...")
    
    # Navigate to team page and click table view
    page.get_by_role("button", name=team_name).last.click()
    # page.locator(f"a[role='button'][href*={team_name.split()[0].lower()}]").click()  
    try:
        page.get_by_role("button", name="Taula").click(timeout=500)
    except:
        pass
    page.wait_for_selector("table.table.no-swipe", timeout=1000)
    
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

            change_raw = safe_get_attribute(row.locator("increment"), "aria-label", "0%")
            change = re.sub(r"[^\d\.-]", "", change_raw)

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
    if team_name == 'General "Hansi” Topete':
        team_name = 'General Hansi Topete'
    filename = f"csvs/teams/team_{team_name.replace(' ', '_').replace('/', '_')}.csv"
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
        page.get_by_role("button", name="Taula").click(timeout=3000)
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
        page.get_by_role("button", name="Taula").click()
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

def extract_market_players(page) -> pd.DataFrame:
    """Extract player data from the market table view"""
    print("\nExtracting market players...")
    
    # Navigate to market
    page.goto("https://biwenger.as.com/market")
    
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
            
            increase_cell = row.locator("td").nth(4)  # % increase column
            increase = safe_inner_text(increase_cell, 0)
            
            fit_cell = row.locator("td").nth(5)  # Fit
            fit = safe_inner_text(fit_cell, "Yes")
            
            demand_cell = row.locator("td").nth(6)  # Demand column
            demand = safe_inner_text(demand_cell, "0")
            
            owner_cell = row.locator("td").nth(7)  # Owner column
            owner = safe_inner_text(owner_cell, "Free Agent")
            
            sale_price_cell = row.locator("td").nth(8)  # Last sale price
            sale_price = safe_inner_text(sale_price_cell, "0").replace("€", "").strip()

            all_rows.append({
                "position": position,
                "club": club,
                "name": name,
                "price": price,
                "owner": owner,
                "last_sale": sale_price,
                "demand": demand,
                "this_season_pts": this_season_pts,
                "last_season_pts": last_season_pts,
                "scraped_at": pd.Timestamp.now().strftime("%Y-%m-%d")
            })
            print(all_rows[-1])  # Print the last row added for debugging
            
        except Exception as e:
            print(f"⚠️ Error processing market row: {e}")
            continue
    
    df = pd.DataFrame(all_rows)
    
    # Clean numerical columns
    numeric_cols = ["price", "demand", "this_season_pts", "last_season_pts"]
    for col in numeric_cols:
        df[col] = df[col].str.replace(r"[^\d\.]", "", regex=True)
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    filename = "csvs/market/market_players.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Saved {len(df)} market players to {filename}")
    return df

import time
import json
import json, time
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

def get_all_posts(page, max_scrolls=5, initial_wait=3, load_timeout_ms=6000):
    seen = set()
    all_posts = []

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
        return []

    last_count = post_locator.count()
    print(f"Start: {last_count} posts")

    for i in range(max_scrolls):
        # Collect what we currently have
        count_now = post_locator.count()
        print(f"Scroll {i+1} (before scroll): {count_now} posts")

        for idx in range(count_now):
            title = post_locator.nth(idx)
            post_data = title.inner_text().split("\n")
            post_id = json.dumps(post_data, ensure_ascii=False)
            if post_id not in seen:
                seen.add(post_id)
                all_posts.append(post_data)

            # Scroll down to trigger loading more
            last_post = page.locator("league-board-post").last
            last_post.scroll_into_view_if_needed()

            # Update last_count for the next loop
            last_count = post_locator.count()
            print(f"Scroll {i+1} (after load): {last_count} posts")

    with open("unique_posts.json", "w", encoding="utf-8") as f:
        json.dump(all_posts, f, ensure_ascii=False, indent=2)
    print(f"✅ Saved {len(all_posts)} unique posts to unique_posts.json")
    return all_posts

# Usage in your run() function:
def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(
        locale="ca-ES",
        extra_http_headers={"Accept-Language": "ca-ES,ca;q=0.9"}
    )
    page = context.new_page()

    # Login
    login(page)
    
    # Get all post titles
    # get_all_posts(page, max_scrolls=20)
 
    # Get all rival teams
    rival_teams = get_rival_teams(page)
    print(f"\nFound {len(rival_teams)} rival teams:")
    for team in rival_teams:
        print(f"{team['position']} - {team['name']} ({team['points']} pts)")
    
    # Extract players for each rival team
    for team in rival_teams:
        print(f"\nProcessing team: {team['name']} at position {team['position']}")
        extract_team_players(page, team["name"])
        # Go back to league view
        page.goto('https://biwenger.as.com/league')  
    
    # # Extract all players
    # print("\nExtracting all players...")
    # all_players_df = extract_all_players(page)
    # print(f"Extracted {len(all_players_df)} players → players.csv")
    
    #extract market players
    market_players_df = extract_market_players(page)
    print(f"Extracted {len(market_players_df)} market players → market_players.csv")

    # extract player probabilities
    
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
    # with sync_playwright() as playwright:
    #     run(playwright)
    get_starting_player_data()
