"""Exports the dashboard as a fully static site (docs/) for GitHub
Pages. /api/data is just a once-daily SQLite query, so instead of
paying for an always-on host to serve it live, the daily GitHub Action
freezes today's snapshot to a JSON file and republishes the same
static HTML/CSS/JS alongside it — no server, no secrets exposed
publicly (scraping stays private in the Action; only the aggregated
output goes public).

Read-only: this covers the dashboard, not the write actions (renewal
already runs unattended in the Action; on-demand bidding from the
static site is a separate, not-yet-built feature that needs a real
backend, since a static page can't make an authenticated call with
Biwenger credentials on its own).
"""
import json
import os
import shutil
import sqlite3

import dashboard_data

OUT_DIR = "docs"

STATIC_MODE_SCRIPT = """    <script>
        window.__staticMode = true;
        window.__selectedDate = {date_json};
    </script>
"""


def main():
    conn = sqlite3.connect('data/biwenger_data.db')
    conn.row_factory = sqlite3.Row
    dates = dashboard_data.get_available_dates(conn)
    if not dates:
        print("No scraped data yet — nothing to export")
        conn.close()
        return

    date = dates[0]
    data = dashboard_data.build_dashboard_data(conn, date)
    conn.close()

    os.makedirs(f"{OUT_DIR}/static/css", exist_ok=True)
    os.makedirs(f"{OUT_DIR}/static/js", exist_ok=True)

    with open(f"{OUT_DIR}/data.json", "w") as f:
        json.dump(data, f)

    shutil.copy("static/css/style.css", f"{OUT_DIR}/static/css/style.css")
    shutil.copy("static/js/dashboard.js", f"{OUT_DIR}/static/js/dashboard.js")

    html = open("templates/dashboard.html").read()
    # Strip the two Jinja-templated bits: asset URLs become plain
    # relative paths (no Flask url_for on a static host), and the
    # inline date-init script is replaced with a static-mode flag that
    # tells dashboard.js to fetch the frozen data.json instead of
    # calling /api/data.
    html = html.replace(
        "{{ url_for('static', filename='css/style.css') }}",
        "static/css/style.css",
    )
    html = html.replace(
        "{{ url_for('static', filename='js/dashboard.js') }}",
        "static/js/dashboard.js",
    )
    old_script = (
        "    <script>\n"
        "        window.__availableDates = {{ available_dates|tojson|safe }};\n"
        "        window.__selectedDate = {{ selected_date|tojson|safe }};\n"
        "    </script>\n"
    )
    if old_script not in html:
        raise RuntimeError(
            "templates/dashboard.html's date-init <script> block didn't match "
            "what export_static.py expects to replace — the template changed "
            "shape, update this string alongside it."
        )
    html = html.replace(old_script, STATIC_MODE_SCRIPT.format(date_json=json.dumps(date)))

    with open(f"{OUT_DIR}/index.html", "w") as f:
        f.write(html)

    # GitHub Pages otherwise runs everything through Jekyll, which
    # ignores/breaks files starting with an underscore among other
    # things this project doesn't use but costs nothing to guard against.
    open(f"{OUT_DIR}/.nojekyll", "w").close()

    print(f"Exported static site to {OUT_DIR}/ (date={date})")


if __name__ == "__main__":
    main()
