from flask import Flask, render_template, request, jsonify
import sqlite3
import os

import dashboard_data

app = Flask(__name__)

@app.route('/')
def dashboard():
    conn = sqlite3.connect('data/biwenger_data.db')
    dates = dashboard_data.get_available_dates(conn)
    conn.close()
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
    data = dashboard_data.build_dashboard_data(conn, date)
    conn.close()

    return jsonify(data)

if __name__ == '__main__':
    # 5000 is macOS AirPlay Receiver's default port — use 5001 instead so
    # this doesn't collide with it out of the box.
    app.run(debug=True, port=int(os.getenv('PORT', 5001)))
