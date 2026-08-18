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

@app.route('/')
def dashboard():
    dates = get_available_dates()
    selected_date = request.args.get('date', dates[0] if dates else None)

    return render_template(
        'dashboard.html',
        available_dates=dates,
        selected_date=selected_date,
    )

@app.route('/api/data')
def get_data():
    date = request.args.get('date')
    if not date:
        return jsonify({'error': 'Date parameter required'}), 400

    conn = sqlite3.connect('data/biwenger_data.db')
    conn.row_factory = sqlite3.Row

    # --- League standings ---
    standings = pd.read_sql(
        """
        SELECT position, name, points, value_change, is_me
        FROM teams
        WHERE scraped_at LIKE ?    -- match 'YYYY-MM-DD%'
        ORDER BY position
        """,
        conn, params=(f"{date}%",)
    )
    standings = standings.rename(columns={'position': 'pos', 'name': 'team'})

    # --- Market ---
    market = pd.read_sql(
        """
        SELECT m.position, m.club, m.name, m.price, m.change, m.status, m.recent_pts,
               m.this_season_pts, m.last_season_pts,
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

    # --- Team valuations summary ---
    # Group by team_id (stable across accent/casing variants) but display
    # the human-readable team name, not the normalized slug.
    teams_summary = pd.read_sql(
        """
        SELECT tp.team_id, MAX(tp.team) AS team_name, COUNT(*) AS player_count,
               SUM(tp.price) AS total_value,
               MAX(tb.ledger_balance) AS balance,
               MAX(tb.is_me) AS is_me,
               MAX(t.value_change) AS value_change
        FROM team_players tp
        LEFT JOIN team_balance tb
          ON tb.team_id = tp.team_id AND tb.scraped_at LIKE ?
        LEFT JOIN teams t
          ON t.name = tp.team AND t.scraped_at LIKE ?
        WHERE tp.scraped_at LIKE ?
        GROUP BY tp.team_id
        ORDER BY total_value DESC
        """,
        conn, params=(f"{date}%", f"{date}%", f"{date}%")
    )

    # Position breakdown per team (GK/DEF/MID/FWD counts) — dual-position
    # players ("Defender/Midfielder") count toward each position they cover.
    positions_df = pd.read_sql(
        "SELECT team_id, position FROM team_players WHERE scraped_at LIKE ?",
        conn, params=(f"{date}%",)
    )
    POSITION_LABELS = {'Goalkeeper': 'GK', 'Defender': 'DEF', 'Midfielder': 'MID', 'Forward': 'FWD'}
    pos_counts = {}
    for _, row in positions_df.iterrows():
        counts = pos_counts.setdefault(row['team_id'], {'GK': 0, 'DEF': 0, 'MID': 0, 'FWD': 0})
        for token in str(row['position']).split('/'):
            label = POSITION_LABELS.get(token.strip())
            if label:
                counts[label] += 1

    teams_summary = teams_summary.copy()
    teams_summary.loc[:, 'positions'] = teams_summary['team_id'].map(pos_counts)
    # Biwenger's own max-bid formula, confirmed against the live bid modal:
    # balance + 25% of total squad value (checked to the euro:
    # €368,300 + 0.25 x €68,050,000 = €17,380,800, matched exactly).
    teams_summary.loc[:, 'max_bid'] = teams_summary['balance'] + 0.25 * teams_summary['total_value']
    teams_summary = teams_summary.rename(
        columns={'team_name': 'team', 'player_count': 'players'}
    )

    # --- Team players for every team (the dashboard expands rosters inline
    # under each team's row rather than filtering to one team at a time) ---
    base_team_players_sql = """
        SELECT tp.team_id, tp.position, tp.club, tp.name, tp.price, tp.change, tp.this_season_pts,
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
    base_team_players_sql += " ORDER BY tp.team_id, tp.price DESC"

    team_players = pd.read_sql(base_team_players_sql, conn, params=params)

    # --- My trades: current holdings (unrealized profit) and completed
    # sales (realized profit), for the "My Trades" tab. Only meaningful
    # for the logged-in user's own team (is_me) — that's the only account
    # whose purchase history the forum ledger can be trusted to be complete
    # for from the moment we started scraping.
    accent_fold = lambda col: (
        f"LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({col},'á','a'),'é','e'),'í','i'),'ó','o'),'ñ','n'))"
    )
    my_holdings = pd.read_sql(
        f"""
        SELECT op.player, op.buy_price, op.count, tp.price AS current_price,
               tp.price - op.buy_price AS profit, tp.club, tp.position
        FROM open_positions op
        JOIN team_balance tb ON tb.team_id = op.team_id AND tb.is_me = 1 AND tb.scraped_at LIKE ?
        JOIN team_players tp ON tp.team_id = op.team_id AND tp.scraped_at LIKE ?
          AND {accent_fold('tp.name')} = {accent_fold('op.player')}
        WHERE op.scraped_at LIKE ?
        ORDER BY profit DESC
        """,
        conn, params=(f"{date}%", f"{date}%", f"{date}%")
    )

    my_sales = pd.read_sql(
        """
        SELECT rt.player, rt.buy_price, rt.sell_price, rt.profit
        FROM realized_trades rt
        JOIN team_balance tb ON tb.team_id = rt.team_id AND tb.is_me = 1 AND tb.scraped_at LIKE ?
        WHERE rt.scraped_at LIKE ? AND rt.profit IS NOT NULL
        ORDER BY rt.profit DESC
        """,
        conn, params=(f"{date}%", f"{date}%")
    )

    conn.close()

    return jsonify({
        'standings': standings.to_dict('records'),
        'market': market.to_dict('records'),
        'teams': teams_summary.to_dict('records'),
        'team_players': team_players.to_dict('records'),
        'my_holdings': my_holdings.to_dict('records'),
        'my_sales': my_sales.to_dict('records'),
        'date': date
    })

if __name__ == '__main__':
    # 5000 is macOS AirPlay Receiver's default port — use 5001 instead so
    # this doesn't collide with it out of the box.
    app.run(debug=True, port=int(os.getenv('PORT', 5001)))