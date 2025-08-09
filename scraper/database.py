import sqlite3
from datetime import datetime
import pandas as pd
import glob
import os

def init_db():
    conn = sqlite3.connect('biwenger_data.db')
    cursor = conn.cursor()
    
    # Players table (all players in the game)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    
    # Market table (current market status)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS market (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    
    # Teams table (rival teams)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position INTEGER,
        name TEXT,
        points INTEGER,
        scraped_at TIMESTAMP
    )''')
    
    # Team players table (players owned by each team)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS team_players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_id INTEGER,
        player_id INTEGER,
        scraped_at TIMESTAMP,
        FOREIGN KEY(team_id) REFERENCES teams(id),
        FOREIGN KEY(player_id) REFERENCES players(id)
    )''')
    
    # Posts table (forum/board posts)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT,
        scraped_at TIMESTAMP
    )''')
    
    # NEW: Player probabilities table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS player_probabilities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_name TEXT,
        team_name TEXT,
        probability TEXT,
        match_date TEXT,
        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    
    conn.commit()
    conn.close()

def remove_duplicates():
    conn = sqlite3.connect('data/biwenger_data.db')
    cursor = conn.cursor()
    
    try:
        # Create temporary tables without duplicates
        cursor.execute('''
        CREATE TABLE temp_probabilities AS
        SELECT MIN(rowid) as rowid, player_name, team_name, probability, match_date, scraped_at
        FROM player_probabilities
        GROUP BY player_name, team_name, probability, match_date
        ''')
        
        # Replace original with cleaned data
        cursor.execute('DROP TABLE player_probabilities')
        cursor.execute('ALTER TABLE temp_probabilities RENAME TO player_probabilities')
        
        # Same for market table
        cursor.execute('''
        CREATE TABLE temp_market AS
        SELECT MIN(rowid) as rowid, position, club, name, price, owner, 
               last_sale, demand, this_season_pts, last_season_pts, scraped_at
        FROM market
        GROUP BY position, club, name, price, scraped_at
        ''')
        
        cursor.execute('DROP TABLE market')
        cursor.execute('ALTER TABLE temp_market RENAME TO market')
        
        conn.commit()
        print("Duplicate entries removed successfully")
    except Exception as e:
        print(f"Error removing duplicates: {e}")
        conn.rollback()
    finally:
        conn.close()

import unicodedata

def normalize_text(text):
    """Normalize text by removing accents and special characters"""
    if not text:
        return ''
    text = str(text)
    # Normalize to decomposed form and remove diacritical marks
    text = unicodedata.normalize('NFKD', text)
    text = ''.join([c for c in text if not unicodedata.combining(c)])
    # Replace specific Spanish characters
    replacements = {
        'ñ': 'n',
        'Ñ': 'N',
        'ü': 'u',
        'Ü': 'U'
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.lower().strip()


def normalize_existing_names():
    conn = sqlite3.connect('data/biwenger_data.db')
    cursor = conn.cursor()
    
    try:
        # Normalize special characters but preserve capitalization
        cursor.execute('''
        UPDATE player_probabilities 
        SET player_name = 
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                player_name, 
                'á', 'a'), 'é', 'e'), 'í', 'i'), 'ó', 'o'), 'ñ', 'n')
        ''')
        
        cursor.execute('''
        UPDATE player_probabilities 
        SET team_name = 
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                team_name, 
                'á', 'a'), 'é', 'e'), 'í', 'i'), 'ó', 'o'), 'ñ', 'n')
        ''')
        
        cursor.execute('''
        UPDATE market 
        SET name = 
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                name, 
                'á', 'a'), 'é', 'e'), 'í', 'i'), 'ó', 'o'), 'ñ', 'n')
        ''')
        
        cursor.execute('''
        UPDATE market 
        SET club = 
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                club, 
                'á', 'a'), 'é', 'e'), 'í', 'i'), 'ó', 'o'), 'ñ', 'n')
        ''')
        
        cursor.execute('''
        UPDATE team_players 
        SET name = 
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                name, 
                'á', 'a'), 'é', 'e'), 'í', 'i'), 'ó', 'o'), 'ñ', 'n')
        ''')
        
        cursor.execute('''
        UPDATE team_players 
        SET club = 
            REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                club, 
                'á', 'a'), 'é', 'e'), 'í', 'i'), 'ó', 'o'), 'ñ', 'n')
        ''')
        
        conn.commit()
        print("Existing names normalized successfully (preserving capitalization)")
    except Exception as e:
        print(f"Error normalizing names: {e}")
        conn.rollback()
    finally:
        conn.close()
        
if __name__ == "__main__":
    normalize_existing_names()