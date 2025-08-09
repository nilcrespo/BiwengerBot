# storage.py
import sqlite3
from datetime import datetime

def save_players(players_df):
    conn = sqlite3.connect('biwenger_data.db')
    players_df['scraped_at'] = datetime.now()
    players_df.to_sql('players', conn, if_exists='append', index=False)
    conn.close()

def save_market_players(market_df):
    conn = sqlite3.connect('biwenger_data.db')
    market_df['scraped_at'] = datetime.now()
    market_df.to_sql('market', conn, if_exists='append', index=False)
    conn.close()

def save_teams(teams_df):
    conn = sqlite3.connect('biwenger_data.db')
    teams_df['scraped_at'] = datetime.now()
    teams_df.to_sql('teams', conn, if_exists='append', index=False)
    conn.close()

def save_posts(posts_list):
    conn = sqlite3.connect('biwenger_data.db')
    cursor = conn.cursor()
    now = datetime.now()
    for post in posts_list:
        cursor.execute('''
        INSERT INTO posts (content, scraped_at) VALUES (?, ?)
        ''', (json.dumps(post), now))
    conn.commit()
    conn.close()