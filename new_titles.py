import os
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone

# Toptoon's new releases page
URL = "https://toptoon.com/new"
STATE_FILE = "seen_series.json"

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("Webhook not configured.")
        return
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9"
    }
    
    response = requests.get(URL, headers=headers, timeout=15)
    soup = BeautifulSoup(response.text, "html.parser")
    
    current_series = {}
    
    # Isolate the href attributes to find episode list links
    for link in soup.select("a[href^='/comic/ep_list/']"):
        href = link.get("href")
        slug = href.split("/")[-1]
        
        # Grab the title from the container
        title_tag = link.select_one(".tit, .title, p") 
        title = title_tag.get_text(strip=True) if title_tag else slug
        
        current_series[slug] = {
            "title": title,
            "url": f"https://toptoon.com{href}"
        }
        
    # Load previously seen series
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            seen = json.load(f)
    else:
        seen = []
        
    new_slugs = [slug for slug in current_series.keys() if slug not in seen]
    
    # On the very first run, just establish the baseline
    if not seen:
        print("Initial run. Setting baseline of existing series.")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(current_series.keys()), f)
        return
        
    for slug in new_slugs:
        data = current_series[slug]
        embed = {
            "title": f"🚨 BRAND NEW SERIES: {data['title'][:200]}",
            "url": data["url"],
            "color": 0x00FF00,
            "description": f"A completely new manhwa just dropped on Toptoon!\n\n**Slug:** `{slug}`\n[Check it out here]({data['url']})",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        requests.post(webhook, json={"username": "New Title Sniper", "embeds": [embed]})
        seen.append(slug)
        time.sleep(1) # Prevent Discord rate limits
        
    # Save the updated master list
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)

if __name__ == "__main__":
    main()
