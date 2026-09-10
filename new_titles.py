"""Poll Toptoon for brand new series and notify Discord."""
import os
import json
import time
import logging
import re
from pathlib import Path
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("new_series_tracker")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

BASE = "https://toptoon.com"
TARGET_URL = f"{BASE}/new"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
STATE_FILE = Path("new_series_state.json")

def load_state(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_state(path: Path, state: list) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")

def get_webhook() -> str:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        raise ValueError("DISCORD_WEBHOOK_URL is missing.")
    return webhook

def send_discord(webhook: str, title: str, comic_id: str, cover_url: str):
    url = f"{BASE}/comic/ep_list/{comic_id}"
    embed = {
        "title": "🚨 New Series Released on Toptoon!",
        "color": 0x00FF00,
        "description": f"**{title}** just launched on Toptoon.\n[Click here to open series]({url})",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Toptoon New Series Tracker"}
    }
    if cover_url and cover_url.startswith("http"):
        embed["thumbnail"] = {"url": cover_url}
        
    payload = {
        "username": "Toptoon Release Tracker",
        "embeds": [embed]
    }
    
    requests.post(webhook, json=payload, timeout=10)
    time.sleep(1.5) # Prevent Discord rate-limiting

def main():
    webhook = get_webhook()
    state = load_state(STATE_FILE)
    
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": BASE})
    
    try:
        response = session.get(TARGET_URL, timeout=15)
    except requests.RequestException as e:
        LOG.error(f"Network error fetching new series: {e}")
        return

    if response.status_code != 200:
        LOG.error(f"Failed to fetch {TARGET_URL}. Status: {response.status_code}")
        return
        
    soup = BeautifulSoup(response.text, "html.parser")
    new_found = False
    
    # Scan all links on the page pointing to a comic episode list
    for a_tag in soup.find_all("a", href=re.compile(r"^/comic/ep_list/([^/?]+)")):
        href = a_tag.get("href")
        comic_id = href.split("/comic/ep_list/")[-1].split("?")[0]
        
        # Skip if we have already alerted for this series
        if comic_id in state:
            continue
            
        title = comic_id
        cover_url = ""
        img_tag = a_tag.find("img")
        
        if img_tag:
            title = img_tag.get("alt", comic_id).strip() or comic_id
            cover_url = img_tag.get("src", "") or img_tag.get("data-src", "")
            
        if title == comic_id and a_tag.text.strip():
            title = a_tag.text.strip()
            
        LOG.info(f"New series detected: {title} ({comic_id})")
        send_discord(webhook, title, comic_id, cover_url)
        
        state.append(comic_id)
        new_found = True
        
    if new_found:
        save_state(STATE_FILE, state)
    else:
        LOG.info("No new series detected on this run.")

if __name__ == "__main__":
    main()
