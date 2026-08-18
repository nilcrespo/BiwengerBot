import pandas as pd
import sqlite3
import glob
import re
from datetime import datetime, timedelta
import os

def normalize_team_key(name):
    """Stable identifier for a team; mirrors scraper.normalize_team_key.

    Fallback only — CSVs written by the current scraper already carry a
    team_id column. This exists so older-format CSVs without that column
    still key consistently instead of fragmenting by accent/casing.
    """
    text = str(name).lower().strip()
    replacements = {'ñ': 'n', 'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o',
                    'ú': 'u', 'ü': 'u', 'à': 'a', 'è': 'e', 'ò': 'o'}
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = text.title()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text

def migrate_csv_to_db(days_behind=0):
    # Ensure data directory exists
    os.makedirs('data', exist_ok=True)
    
    conn = sqlite3.connect('data/biwenger_data.db')
    cursor = conn.cursor()
    
    # Initialize tables if they don't exist
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS market (
        position TEXT,
        club TEXT,
        name TEXT,
        price REAL,
        change REAL,
        status TEXT,
        owner TEXT,
        last_sale TEXT,
        recent_pts REAL,
        this_season_pts REAL,
        last_season_pts REAL,
        scraped_at TIMESTAMP
    )''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS teams (
        position INTEGER,
        name TEXT,
        points INTEGER,
        team_value REAL,
        value_change REAL,
        num_players INTEGER,
        is_me INTEGER,
        scraped_at TIMESTAMP
    )''')

    # team_players must exist even before the first team CSV is migrated —
    # app.py queries it unconditionally to populate the team filter dropdown.
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS team_players (
        team TEXT,
        team_id TEXT,
        position TEXT,
        club TEXT,
        name TEXT,
        this_season_pts REAL,
        last_season_pts REAL,
        price REAL,
        change REAL,
        status TEXT,
        played INTEGER,
        points_per_match REAL,
        home_pts REAL,
        home_average REAL,
        away_pts REAL,
        away_average REAL,
        scraped_at TIMESTAMP
    )''')

    # NEW: Initialize probabilities table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS player_probabilities (
        player_name TEXT,
        team_name TEXT,
        probability TEXT,
        match_date TEXT,
        scraped_at TIMESTAMP
    )''')
    
    conn.commit()
    
    # Process market CSV
    market_files = glob.glob('csvs/market/market_players.csv')
    for file in market_files:
        try:
            df = pd.read_csv(file)
            print(f"\nProcessing {file} with columns: {list(df.columns)}")
            
            # Ensure only free agents are included. extract_market_players()
            # already filters to free agents, but its owner text goes
            # through the scraper's name-normalizer (title-cases
            # everything), so match case-insensitively rather than the
            # exact literal "Free agent".
            df = df[df['owner'].str.lower() == 'free agent']

            df['scraped_at'] = ((datetime.now()-timedelta(days=days_behind))).strftime('%Y-%m-%d %H:%M:%S')

            # Select only the columns we need. Older CSVs (pre-rename) won't
            # have 'change'/'status'/'recent_pts', or will still have the
            # old 'demand' name — handle either shape gracefully.
            if 'change' not in df.columns:
                df['change'] = 0
            if 'status' not in df.columns:
                df['status'] = 'Fit'
            if 'recent_pts' not in df.columns:
                df['recent_pts'] = df['demand'] if 'demand' in df.columns else 0
            df = df[['position', 'club', 'name', 'price', 'change', 'status', 'owner',
                    'last_sale', 'recent_pts', 'this_season_pts',
                    'last_season_pts', 'scraped_at']]
            
            df.to_sql('market', conn, if_exists='append', index=False)
            print(f"→ Migrated {len(df)} records to market table")
            
        except Exception as e:
            print(f"Error processing {file}: {str(e)}")
    
    # Process league standings
    league_files = glob.glob('csvs/others/league_standings.csv')
    for file in league_files:
        try:
            df = pd.read_csv(file)
            print(f"\nProcessing {file} with columns: {list(df.columns)}")
            
            # Rename columns if necessary
            if 'pl.' in df.columns:
                df = df.rename(columns={'pl.': 'position'})
            if 'points' not in df.columns:
                df['points'] = 0

            df['scraped_at'] = (datetime.now()-timedelta(days=days_behind)).strftime('%Y-%m-%d %H:%M:%S')

            df.to_sql('teams', conn, if_exists='append', index=False)
            print(f"→ Migrated {len(df)} records to teams table")
            
        except Exception as e:
            print(f"Error processing {file}: {str(e)}")
    
    # Process team players
    team_files = glob.glob('csvs/teams/team_*.csv')
    for file in team_files:
        df = pd.read_csv(file)
        team_name = df['team'].iloc[0]
        team_id = df['team_id'].iloc[0] if 'team_id' in df.columns else normalize_team_key(team_name)
        print(f"\nProcessing {file} for team_players of {team_name!r} (team_id={team_id!r})")

        # Build and clean your players_df ... (keep 'team' — it's the
        # human-readable display name; app.py needs it alongside team_id)
        players_df = df.copy()
        players_df['team_id'] = team_id
        players_df['scraped_at'] = ((datetime.now()-timedelta(days=days_behind))).strftime('%Y-%m-%d %H:%M:%S')

        # Append into team_players
        players_df.to_sql('team_players', conn, if_exists='append', index=False)
        print(f"  → Saved {len(players_df)} players for '{team_name}' (team_id={team_id})")
    
    # NEW: Process player probabilities
    prob_files = glob.glob('csvs/others/player_probabilities.csv')
    for file in prob_files:
        try:
            df = pd.read_csv(file)
            print(f"\nProcessing {file} with columns: {list(df.columns)}")
            
            df['scraped_at'] = (datetime.now()-timedelta(days=days_behind)).strftime('%Y-%m-%d %H:%M:%S')
            df['match_date'] = (datetime.now()-timedelta(days=days_behind)).strftime('%Y-%m-%d')  # Current date as match date
            
            # Select only the columns we need
            df = df[['Player', 'Team', 'Probability', 'match_date', 'scraped_at']]
            df = df.rename(columns={
                'Player': 'player_name',
                'Team': 'team_name',
                'Probability': 'probability'
            })
            
            df.to_sql('player_probabilities', conn, if_exists='append', index=False)
            print(f"→ Migrated {len(df)} records to player_probabilities table")
            
        except Exception as e:
            print(f"Error processing {file}: {str(e)}")

    # Team balances — computed from the forum-post ledger (season-start
    # budget credit, salaries, market purchases/sales, admin adjustments).
    # Cross-checked against the logged-in user's own live-scraped balance
    # when available, since that's the only team we have ground truth for.
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS team_balance (
        team_id TEXT,
        team_name TEXT,
        ledger_balance REAL,
        actual_balance REAL,
        is_me INTEGER,
        scraped_at TIMESTAMP
    )''')
    conn.commit()

    posts_path = 'unique_posts.json'
    if os.path.exists(posts_path):
        try:
            import json
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "money_left", "money_movements_history/money_left.py"
            )
            money_left = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(money_left)

            with open(posts_path, encoding='utf-8') as f:
                posts = json.load(f)
            balances, ledger = money_left.compute_balances(posts)

            # Which team_id is mine, and what does Biwenger say my balance
            # actually is (ground truth for validation)?
            my_team_id = None
            if os.path.exists('csvs/others/league_standings.csv'):
                standings_df = pd.read_csv('csvs/others/league_standings.csv')
                if 'is_me' in standings_df.columns:
                    me_rows = standings_df[standings_df['is_me'] == True]
                    if len(me_rows):
                        my_team_id = normalize_team_key(me_rows.iloc[0]['name'])

            actual_balance = None
            if os.path.exists('csvs/others/my_balance.csv'):
                my_bal_df = pd.read_csv('csvs/others/my_balance.csv')
                if len(my_bal_df):
                    actual_balance = float(my_bal_df.iloc[0]['balance'])

            now = (datetime.now() - timedelta(days=days_behind)).strftime('%Y-%m-%d %H:%M:%S')
            rows = []
            for team_name, bal in balances.items():
                team_id = normalize_team_key(team_name)
                is_me = (team_id == my_team_id)
                rows.append({
                    'team_id': team_id,
                    'team_name': team_name,
                    'ledger_balance': bal,
                    'actual_balance': actual_balance if is_me else None,
                    'is_me': is_me,
                    'scraped_at': now,
                })
            pd.DataFrame(rows).to_sql('team_balance', conn, if_exists='append', index=False)
            print(f"→ Migrated {len(rows)} team balances")

            if my_team_id and actual_balance is not None:
                mine = next((r for r in rows if r['team_id'] == my_team_id), None)
                if mine:
                    diff = mine['ledger_balance'] - actual_balance
                    status = "MATCH" if abs(diff) < 1 else f"MISMATCH (diff €{diff:,.0f})"
                    print(f"  ↳ balance validation for {mine['team_name']}: "
                          f"ledger=€{mine['ledger_balance']:,.0f} actual=€{actual_balance:,.0f} [{status}]")

            # Trade history — reconstructed buy/sell pairs per player from
            # the same ledger, for the "my purchases / profit" tab.
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS realized_trades (
                team_id TEXT,
                team_name TEXT,
                player TEXT,
                buy_price REAL,
                sell_price REAL,
                profit REAL,
                scraped_at TIMESTAMP
            )''')
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS open_positions (
                team_id TEXT,
                team_name TEXT,
                player TEXT,
                buy_price REAL,
                count INTEGER,
                scraped_at TIMESTAMP
            )''')
            conn.commit()

            trades = money_left.compute_trades(ledger)
            realized_rows, open_rows = [], []
            for team_name, t in trades.items():
                team_id = normalize_team_key(team_name)
                for r in t["realized"]:
                    realized_rows.append({
                        'team_id': team_id, 'team_name': team_name,
                        'player': r['player'], 'buy_price': r['buy_price'],
                        'sell_price': r['sell_price'], 'profit': r['profit'],
                        'scraped_at': now,
                    })
                for player, info in t["open"].items():
                    open_rows.append({
                        'team_id': team_id, 'team_name': team_name,
                        'player': player, 'buy_price': info['buy_price'],
                        'count': info['count'], 'scraped_at': now,
                    })
            if realized_rows:
                pd.DataFrame(realized_rows).to_sql('realized_trades', conn, if_exists='append', index=False)
            if open_rows:
                pd.DataFrame(open_rows).to_sql('open_positions', conn, if_exists='append', index=False)
            print(f"→ Migrated {len(realized_rows)} realized trades, {len(open_rows)} open positions")

            # Bid history — per-transaction market price vs. number of
            # competing bids, plus a price-bucketed aggregate. Underlying
            # data for a future "how much should I bid" recommender (not
            # built here); raw rows in bid_history let that recommender
            # work at whatever precision it needs, bid_history_buckets is
            # a coarse precomputed summary for quick lookups.
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS bid_history (
                player TEXT,
                position TEXT,
                price REAL,
                bids INTEGER,
                scraped_at TIMESTAMP
            )''')
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS bid_history_buckets (
                bucket TEXT,
                count INTEGER,
                avg_bids REAL,
                median_bids REAL,
                min_price REAL,
                max_price REAL,
                scraped_at TIMESTAMP
            )''')
            conn.commit()

            bid_transactions = money_left.compute_bid_history(posts)
            bid_rows = [{
                'player': t['player'], 'position': t['position'],
                'price': t['price'], 'bids': t['bids'], 'scraped_at': now,
            } for t in bid_transactions]
            if bid_rows:
                pd.DataFrame(bid_rows).to_sql('bid_history', conn, if_exists='append', index=False)

            bucket_rows = money_left.compute_bid_buckets(bid_transactions)
            for b in bucket_rows:
                b['scraped_at'] = now
            if bucket_rows:
                pd.DataFrame(bucket_rows).to_sql('bid_history_buckets', conn, if_exists='append', index=False)
            print(f"→ Migrated {len(bid_rows)} bid history transactions across {len(bucket_rows)} price buckets")
        except Exception as e:
            print(f"Error computing team balances/trades: {str(e)}")

    conn.close()
    print("\nMigration complete!")

if __name__ == '__main__':
    migrate_csv_to_db()