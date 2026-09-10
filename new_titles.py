"""Poll Toptoon's completed/new works page and notify Discord of new series."""
import argparse
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("new_series_tracker")
BASE = "https://toptoon.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 "
    "ToptoonReleaseTracker/1.0"
)

def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("seen_slugs", []))

def save_state(path: Path, slugs: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        tmp = Path(handle.name)
        json.dump({"seen_slugs": sorted(list(slugs))}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)

def get_html(url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Referer": f"{BASE}/"}
    response = requests.get(url, headers=headers, timeout=(10, 30))
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text

def get_series_info(slug: str) -> dict:
    url = f"{BASE}/comic/ep_list/{slug}"
    try:
        html = get_html(url)
    except requests.RequestException:
        url = f"{BASE}/complete/ep_list/{slug}"
        try:
            html = get_html(url)
        except requests.RequestException:
            return {"title": slug, "thumbnail": None, "url": url}

    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.select_one('meta[property="og:title"]')
    title = title_tag.get("content", "").strip() if title_tag else slug
    title = re.sub(r"^\s*\[탑툰\]\s*", "", title)
    image_tag = soup.select_one('meta[property="og:image"]')
    thumbnail = image_tag.get("content") if image_tag else None
    
    return {"title": title, "thumbnail": thumbnail, "url": url}

def send_discord(url: str, info: dict) -> None:
    embed = {
        "title": info["title"][:256],
        "url": info["url"],
        "color": 0x3498db,
        "description": "A new series has been detected on Toptoon!",
        "fields": [{"name": "Link", "value": f"[Open Series]({info['url']})"}],
        "footer": {"text": "Toptoon New Series Tracker"}
    }
    if info.get("thumbnail") and info["thumbnail"].startswith("https"):
        embed["thumbnail"] = {"url": info["thumbnail"]}
        
    payload = {"username": "Toptoon Release Tracker", "embeds": [embed]}
    response = requests.post(url, params={"wait": "true"}, json=payload, timeout=(10, 30))
    response.raise_for_status()

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("new_series_state.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not args.dry_run and not webhook:
        LOG.error("DISCORD_WEBHOOK_URL is not set.")
        return 1

    seen = load_state(args.state)
    is_baseline = len(seen) == 0

    try:
        html = get_html(f"{BASE}/complete")
        
        # Extract series slugs from href paths
        slugs = set(re.findall(r"/(?:comic|complete)/ep_list/([A-Za-z0-9_-]+)", html))
        
        if not slugs:
            LOG.warning("No series links found. Toptoon HTML structure may have changed.")
            return 1

        new_slugs = slugs - seen

        if is_baseline:
            LOG.info(f"Initialized baseline with {len(slugs)} series. No alerts sent.")
            save_state(args.state, slugs)
            return 0

        LOG.info(f"Found {len(new_slugs)} new series.")
        
        for slug in new_slugs:
            if args.dry_run:
                LOG.info(f"Would notify new series: {slug}")
            else:
                info = get_series_info(slug)
                try:
                    send_discord(webhook, info)
                    LOG.info(f"Notified: {info['title']}")
                    time.sleep(2)  # Avoid hitting Discord rate limits
                except requests.RequestException as exc:
                    LOG.error(f"Failed to notify {slug}: {exc}")
                    continue
            
            seen.add(slug)
            save_state(args.state, seen)

    except requests.RequestException as exc:
        LOG.error(f"Network error: {exc}")
        return 1
    except Exception as exc:
        LOG.error(f"Error: {exc}")
        return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
