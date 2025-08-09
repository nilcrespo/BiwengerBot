# scheduler.py
import schedule
import time
from scraper import run_scraper

def daily_job():
    print("Running Biwenger scraper...")
    run_scraper()  # Your modified run function

# Schedule daily at 8 AM
schedule.every().day.at("10:00").do(daily_job)

while True:
    schedule.run_pending()
    time.sleep(60)  # Check every minute