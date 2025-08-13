import sqlite3
import pandas as pd

def check_data():
    conn = sqlite3.connect('data/biwenger_data.db')
    
    data = pd.read_sql("SELECT * FROM team_players LIMIT 5", conn)
    print("Sample data from team_players:")
    print(data)
    
    # Check available dates
    dates = pd.read_sql("""
        SELECT DISTINCT DATE(scraped_at) as date 
        FROM team_players
        ORDER BY date DESC
    """, conn)
    print("Available dates in team_players:")
    print(dates)
    
    # Check teams for a specific date
    date_to_check = '2025-07-25'
    teams = pd.read_sql(f"""
        SELECT DISTINCT team_id 
        FROM team_players 
        WHERE DATE(scraped_at) = '{date_to_check}'
    """, conn)
    print(f"\nTeams available on {date_to_check}:")
    print(teams)
    
    # Check Patsi F.C data
    patsi_data = pd.read_sql(f"""
        SELECT * FROM team_players 
        WHERE team_id = 'Patsi F.C' 
        AND DATE(scraped_at) = '{date_to_check}'
        LIMIT 5
    """, conn)
    print("\nSample Patsi F.C data:")
    print(patsi_data)
    
    conn.close()

if __name__ == '__main__':
    check_data()