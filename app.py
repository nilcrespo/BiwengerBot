from flask import Flask, render_template, request, jsonify
import sqlite3
import pandas as pd
import os
from datetime import datetime

app = Flask(__name__)

def get_available_dates():
    conn = sqlite3.connect('data/biwenger_data.db')
    query = """
    SELECT DISTINCT DATE(scraped_at) as date 
    FROM (
        SELECT scraped_at FROM market
        UNION SELECT scraped_at FROM team_players
        UNION SELECT scraped_at FROM teams
    )
    ORDER BY date DESC
    """
    dates = pd.read_sql(query, conn)['date'].tolist()
    conn.close()
    return dates

def get_available_teams():
    conn = sqlite3.connect('data/biwenger_data.db')
    query = """
    SELECT DISTINCT team_id 
    FROM team_players
    ORDER BY team_id
    """
    teams = pd.read_sql(query, conn)['team_id'].tolist()
    conn.close()
    return teams

@app.route('/')
def dashboard():
    dates = get_available_dates()
    teams = get_available_teams()
    selected_date = request.args.get('date', dates[0] if dates else None)
    selected_team = request.args.get('team', teams[0] if teams else None)
    
    return render_template(
        'dashboard.html',
        available_dates=dates,
        available_teams=teams,
        selected_date=selected_date,
        selected_team=selected_team
    )

@app.route('/api/data')
def get_data():
    date = request.args.get('date')
    team = request.args.get('team')
    if not date:
        return jsonify({'error': 'Date parameter required'}), 400

    conn = sqlite3.connect('data/biwenger_data.db')
    conn.row_factory = sqlite3.Row

    # --- League standings (keep original SQL; just rename for UI) ---
    standings = pd.read_sql(
        """
        SELECT position, name, points
        FROM teams
        WHERE scraped_at LIKE ?    -- match 'YYYY-MM-DD%'
        ORDER BY position
        """,
        conn, params=(f"{date}%",)
    )
    standings = standings.rename(columns={'position': 'pos', 'name': 'team'})

    # --- Market (unchanged) ---
    market = pd.read_sql(
        """
        SELECT m.position, m.club, m.name, m.price, m.demand, m.this_season_pts,
               COALESCE(pp.probability, '0%') AS probability
        FROM market m
        LEFT JOIN (
            SELECT player_name, team_name, probability
            FROM player_probabilities
            WHERE scraped_at LIKE ? AND probability != '0%'
        ) pp
          ON LOWER(
               REPLACE(REPLACE(REPLACE(REPLACE(m.name,'á','a'),'é','e'),'í','i'),'ó','o')
             ) = LOWER(
               REPLACE(REPLACE(REPLACE(REPLACE(pp.player_name,'á','a'),'é','e'),'í','i'),'ó','o')
             )
         AND LOWER(REPLACE(m.club, ' ', '')) = LOWER(REPLACE(pp.team_name, ' ', ''))
        WHERE m.scraped_at LIKE ?
        ORDER BY m.price DESC
        LIMIT 50
        """,
        conn, params=(f"{date}%", f"{date}%")
    )

    # --- Team valuations summary (keep SQL; just rename for UI) ---
    teams_summary = pd.read_sql(
        """
        SELECT team_id, COUNT(*) AS player_count, SUM(price) AS total_value
        FROM team_players
        WHERE scraped_at LIKE ?
        GROUP BY team_id
        ORDER BY total_value DESC
        """,
        conn, params=(f"{date}%",)
    )
    teams_summary = teams_summary.rename(columns={'team_id': 'team', 'player_count': 'players'})

    # --- Team players (unchanged) ---
    base_team_players_sql = """
        SELECT tp.position, tp.club, tp.name, tp.price, tp.this_season_pts,
               tp.points_per_match, tp.status,
               COALESCE(pp.probability, '0%') AS probability
        FROM team_players tp
        LEFT JOIN (
            SELECT player_name, team_name, probability
            FROM player_probabilities
            WHERE scraped_at LIKE ? AND probability != '0%'
        ) pp
          ON LOWER(
               REPLACE(REPLACE(REPLACE(REPLACE(tp.name,'á','a'),'é','e'),'í','i'),'ó','o')
             ) = LOWER(
               REPLACE(REPLACE(REPLACE(REPLACE(pp.player_name,'á','a'),'é','e'),'í','i'),'ó','o')
             )
         AND LOWER(REPLACE(tp.club, ' ', '')) = LOWER(REPLACE(pp.team_name, ' ', ''))
        WHERE tp.scraped_at LIKE ?
    """
    params = [f"{date}%", f"{date}%"]
    if team:
        base_team_players_sql += " AND tp.team_id = ?"
        params.append(team)
    base_team_players_sql += " ORDER BY tp.price DESC"

    team_players = pd.read_sql(base_team_players_sql, conn, params=params)

    conn.close()

    return jsonify({
        'standings': standings.to_dict('records'),
        'market': market.to_dict('records'),
        'teams': teams_summary.to_dict('records'),
        'team_players': team_players.to_dict('records'),
        'date': date,
        'team': team
    })

if __name__ == '__main__':
    # 5000 is macOS AirPlay Receiver's default port — use 5001 instead so
    # this doesn't collide with it out of the box.
    app.run(debug=True, port=int(os.getenv('PORT', 5001)))