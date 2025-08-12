import re
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
    page.goto("https://biwenger.as.com/")
    try:
        page.get_by_role("button", name="Agree").click(timeout=5000)
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
    page.get_by_role("button", name=team_name).click()
    try:
        page.get_by_role("button", name="Taula").click(timeout=500)
    except:
        pass
    page.wait_for_selector("table tbody tr", timeout=1000)
    
    all_rows = []
    rows = page.locator("table tbody tr").all()
    
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
                "club": club,
                "name": name,
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
    filename = f"team_{team_name.replace(' ', '_').replace('/', '_')}.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Saved {len(df)} players to {filename}")
    return df

def get_rival_teams(page) -> List[Dict]:
    """Get list of all rival teams in the league"""
    page.get_by_role("link", name="Lliga").click()    
    teams = []
    
    team_elements = page.locator("user-card").all()
    
    for team in team_elements:
        name = safe_inner_text(team.locator("h3 > a"), "Unknown Team")
        position = safe_inner_text(team.locator("user-position"), "0")
        points = safe_inner_text(team.locator("div.right ng-star-insterted"), "0")
        teams.append({
            "position": position,
            "name": normalize_player_name(name),
            "points": points.replace(" pl.", "")
        })
    
    # Save league standings
    pd.DataFrame(teams).to_csv("league_standings.csv", index=False)
    return teams

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
                "scraped_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
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
    
    filename = "market_players.csv"
    df.to_csv(filename, index=False)
    print(f"✅ Saved {len(df)} market players to {filename}")
    return df

import time
import json
def get_all_posts(page, max_scrolls=5, scroll_pause=1.5):
    seen = set()
    all_posts = []
    last_count = 0
    for i in range(max_scrolls):
        posts = page.locator("league-board-post").all()
        print(f"Scroll {i+1}: Found {len(posts)} posts")
        for title in posts:
            post_data = title.inner_text().split('\n')
            # Use a hash of the post data as identifier
            post_id = json.dumps(post_data, ensure_ascii=False)
            if post_id not in seen:
                seen.add(post_id)
                all_posts.append(post_data)
                print(post_data)
                print('---')
        page.evaluate("window.scrollBy(0, window.innerHeight*3)")
        time.sleep(scroll_pause)  # Wait for new posts to load and for visibility

        if len(posts) == last_count:
            print("No more new posts loaded.")
            break
        last_count = len(posts)
    # Optionally save all_posts to a file
    with open("unique_posts.json", "w", encoding="utf-8") as f:
        json.dump(all_posts, f, ensure_ascii=False, indent=2)
    print(f"✅ Saved {len(all_posts)} unique posts to unique_posts.json")
    return all_posts

# Usage in your run() function:
def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context()
    page = context.new_page()
    
    # Login
    login(page)
    
    # # Get all post titles
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
        page.get_by_role("button", name="ACEPTO").click()
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

def main():
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
        team_df = scrape_player_probabilities(team)
        all_data = pd.concat([all_data, team_df], ignore_index=True)
        
        # Save progress after each team
        all_data.to_csv("player_probabilities.csv", index=False)
        print(f"Saved data for {team.title()}")
    
    print("\n=== All teams scraped successfully ===")
    print(all_data)
    all_data.to_csv("player_probabilities.csv", index=False)

teams = ['alaves', 'athletic', 'atletico', 'barcelona', 'betis', 'celta', 'elche', 'espanyol', 'getafe', 'girona', 'levante', 'mallorca', 'osasuna', 'rayo-vallecano', 'real-madrid', 'real-oviedo', 'real-sociedad', 'sevilla', 'valencia', 'villarreal'] 
        
if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
    # main()
