"""Standalone entry point for get_starting_player_data() — start
probabilities were previously only ever refreshed by someone manually
uncommenting a line in scraper.py's __main__ block, which meant they'd
gone stale for however long since the last person happened to do that
(confirmed live: player_probabilities had exactly one distinct
scraped_at in the whole table, from a manual run, despite the daily
scrape running every day). This gets its own scheduled workflow instead
of a cadence nobody remembers to trigger by hand.

Deliberately separate from the daily Biwenger scrape (see
get_starting_player_data's own docstring): this hits futbolfantasy.com,
not Biwenger, needs no login/credentials, and a slow or blocked source
here shouldn't be able to hold up the core pipeline.
"""
import os
import sys

# Run as `python scraper/scrape_probabilities.py` from the repo root, same
# as the main scraper — but a direct script invocation only puts this
# file's OWN directory on sys.path, not the repo root, and scraper.py's
# own CSV writes are relative paths that assume the repo root is the
# working directory. Importing scraper.py as a plain top-level module
# (it has no package __init__.py) keeps this consistent with how the
# main workflow already runs scraper.py itself.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scraper import get_starting_player_data  # noqa: E402

if __name__ == "__main__":
    get_starting_player_data()
