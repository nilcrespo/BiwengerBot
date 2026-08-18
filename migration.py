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
        owner TEXT,
        last_sale TEXT,
        demand INTEGER,
        this_season_pts REAL,
        last_season_pts REAL,
        scraped_at TIMESTAMP
    )''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS teams (
        position INTEGER,
        name TEXT,
        points INTEGER,
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
            
            # Ensure only free agents are included
            df = df[df['owner'] == 'Free agent']

            df['scraped_at'] = ((datetime.now()-timedelta(days=days_behind))).strftime('%Y-%m-%d %H:%M:%S')

            # Select only the columns we need
            df = df[['position', 'club', 'name', 'price', 'owner', 
                    'last_sale', 'demand', 'this_season_pts', 
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

        # Build and clean your players_df ...
        players_df = df.drop(columns=['team'])
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

    conn.close()
    print("\nMigration complete!")

if __name__ == '__main__':
    migrate_csv_to_db()