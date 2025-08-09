# analysis.py
import sqlite3
import pandas as pd

def get_player_value_analysis():
    """Analyze player values vs performance"""
    conn = sqlite3.connect('biwenger_data.db')
    query = '''
    SELECT name, position, club, 
           AVG(price) as avg_price,
           AVG(points_per_match) as avg_ppm,
           AVG(price)/NULLIF(AVG(points_per_match), 0) as price_per_point
    FROM players
    GROUP BY name
    ORDER BY price_per_point ASC
    '''
    return pd.read_sql(query, conn)

def get_market_opportunities():
    """Identify undervalued players in market"""
    conn = sqlite3.connect('biwenger_data.db')
    query = '''
    SELECT m.name, m.position, m.club, m.price, 
           p.avg_ppm, p.price_per_point,
           m.price/p.avg_ppm as current_ratio
    FROM market m
    JOIN (
        SELECT name, AVG(points_per_match) as avg_ppm,
               AVG(price)/NULLIF(AVG(points_per_match), 0) as price_per_point
        FROM players
        GROUP BY name
    ) p ON m.name = p.name
    WHERE m.owner = 'Free Agent'
    ORDER BY current_ratio ASC
    '''
    return pd.read_sql(query, conn)