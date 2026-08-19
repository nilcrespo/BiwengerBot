"""Assembles the full dashboard JSON payload — shared by the live Flask
app (app.py) and the static-site exporter (export_static.py) so the two
can't drift apart: both call build_dashboard_data(conn, date) and get
back the exact same dict, one serves it per-request, the other freezes
it to a file once a day.
"""
import pandas as pd

import recommenders
from recommenders import attach_probabilities, accent_fold_sql, to_records, POSITION_LABELS


def get_available_dates(conn):
    query = """
    SELECT DISTINCT DATE(scraped_at) as date
    FROM (
        SELECT scraped_at FROM market
        UNION SELECT scraped_at FROM team_players
        UNION SELECT scraped_at FROM teams
    )
    ORDER BY date DESC
    """
    return pd.read_sql(query, conn)['date'].tolist()


def build_dashboard_data(conn, date):
    # A calendar date can have more than one scrape behind it (manual
    # reruns, retries) — resolve to the single latest run's exact
    # timestamp up front so every query below pulls one consistent
    # snapshot instead of unioning rows across runs. See
    # recommenders.resolve_scraped_at's docstring for the failure mode
    # this fixes (accumulating/duplicated-looking data).
    d = recommenders.resolve_scraped_at(conn, date)

    # --- League standings ---
    standings = pd.read_sql(
        """
        SELECT position, name, points, value_change, is_me
        FROM teams
        WHERE scraped_at LIKE ?
        ORDER BY position
        """,
        conn, params=(d,)
    )
    standings = standings.rename(columns={'position': 'pos', 'name': 'team'})

    # --- Market ---
    market = pd.read_sql(
        """
        SELECT position, club, name, price, change, status, recent_pts,
               this_season_pts, last_season_pts
        FROM market
        WHERE scraped_at LIKE ?
        ORDER BY price DESC
        LIMIT 50
        """,
        conn, params=(d,)
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
        conn, params=(d, d, d)
    )

    # Position breakdown per team (GK/DEF/MID/FWD counts) — dual-position
    # players ("Defender/Midfielder") count toward each position they cover.
    positions_df = pd.read_sql(
        "SELECT team_id, position FROM team_players WHERE scraped_at LIKE ?",
        conn, params=(d,)
    )
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
    team_players = pd.read_sql(
        """
        SELECT team_id, position, club, name, price, change, this_season_pts,
               points_per_match, status
        FROM team_players
        WHERE scraped_at LIKE ?
        ORDER BY team_id, price DESC
        """,
        conn, params=(d,)
    )

    # --- Start-probability matching (see recommenders._find_probability
    # for why this is done in Python rather than a SQL join: exact match
    # after accent folding, falling back to a club-scoped surname/subset
    # match that's only trusted when it resolves to exactly one candidate) ---
    prob_df = pd.read_sql(
        "SELECT player_name, team_name, probability FROM player_probabilities "
        "WHERE scraped_at LIKE ? AND probability != '0%'",
        conn, params=(d,)
    )
    market = attach_probabilities(market, prob_df)
    team_players = attach_probabilities(team_players, prob_df)

    # --- Buy/sell recommenders — shared with the Telegram digest
    # (notify.py) via recommenders.py so the two never drift apart. ---
    buy_recommendations, sell_recommendations = recommenders.build_recommendations(conn, date)

    # --- My trades: current holdings (unrealized profit) and completed
    # sales (realized profit), for the "My Trades" tab. Only meaningful
    # for the logged-in user's own team (is_me) — that's the only account
    # whose purchase history the forum ledger can be trusted to be complete
    # for from the moment we started scraping.
    my_holdings = pd.read_sql(
        f"""
        SELECT op.player, op.buy_price, op.count, tp.price AS current_price,
               tp.price - op.buy_price AS profit, tp.club, tp.position
        FROM open_positions op
        JOIN team_balance tb ON tb.team_id = op.team_id AND tb.is_me = 1 AND tb.scraped_at LIKE ?
        JOIN team_players tp ON tp.team_id = op.team_id AND tp.scraped_at LIKE ?
          AND {accent_fold_sql('tp.name')} = {accent_fold_sql('op.player')}
        WHERE op.scraped_at LIKE ?
        ORDER BY profit DESC
        """,
        conn, params=(d, d, d)
    )

    my_sales = pd.read_sql(
        """
        SELECT rt.player, rt.buy_price, rt.sell_price, rt.profit
        FROM realized_trades rt
        JOIN team_balance tb ON tb.team_id = rt.team_id AND tb.is_me = 1 AND tb.scraped_at LIKE ?
        WHERE rt.scraped_at LIKE ? AND rt.profit IS NOT NULL
        ORDER BY rt.profit DESC
        """,
        conn, params=(d, d)
    )

    return {
        'standings': standings.to_dict('records'),
        'market': market.to_dict('records'),
        'teams': teams_summary.to_dict('records'),
        'team_players': team_players.to_dict('records'),
        'my_holdings': my_holdings.to_dict('records'),
        'my_sales': my_sales.to_dict('records'),
        'buy_recommendations': to_records(buy_recommendations),
        'sell_recommendations': to_records(sell_recommendations),
        'date': date,
    }
