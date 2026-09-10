"""Poll public Toptoon new-series and episode lists and notify Discord. Python 3.10+."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import random
import re
import tempfile
import time
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("tracker")
BASE = "https://toptoon.com"
KST = timezone(timedelta(hours=9))
# Compatibility headers, not a way to bypass authentication or access restrictions.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 "
    "ToptoonReleaseTracker/1.0"
)
SLUG = re.compile(r"[A-Za-z0-9_-]+\Z")
CHAPTER = re.compile(r"(?:제\s*)?(\d+(?:\.\d+)?)\s*화(?:\s|$)")


class TrackerError(Exception):
    """Safe-to-log operational error; never includes webhook credentials."""


class SiteBlocked(TrackerError):
    """Stop all further Toptoon requests for this run."""


@dataclass(frozen=True)
class Episode:
    number: Decimal
    episode_id: str
    url: str
    release: str
    precision: str


@dataclass(frozen=True)
class Series:
    slug: str
    title: str
    thumbnail: str | None
    episodes: tuple[Episode, ...]


@dataclass(frozen=True)
class NewSeries:
    slug: str
    title: str
    thumbnail: str | None

    @property
    def url(self) -> str:
        return f"{BASE}/comic/ep_list/{self.slug}"


def parse_new_series(html: str) -> tuple[NewSeries, ...]:
    """Read only the new-series tab, never completed titles or recommendations."""
    soup = BeautifulSoup(html, "html.parser")
    sections = soup.select('#commonComicList .serial__list[data-name="complete1"]')
    if len(sections) != 1:
        raise TrackerError("New-series tab missing or ambiguous; history remains unchanged.")
    cards = sections[0].select(".serial__item")
    if not cards:
        raise TrackerError("New-series list is empty; refusing to initialize history.")
    found: dict[str, NewSeries] = {}
    for card in cards:
        slug = card.get("data-comic-id", "")
        if not SLUG.fullmatch(slug):
            raise TrackerError("New-series card ID missing or invalid.")
        link = card.select_one("a.serial__link[href]")
        title = card.select_one(".serial__title-text")
        if link is None or title is None or not title.get_text(strip=True):
            raise TrackerError("New-series card selectors changed; history remains unchanged.")
        href = link["href"]
        if href.startswith("/") and not href.startswith("//"):
            href = BASE + href
        if normalize_slug(href) != slug:
            raise TrackerError("New-series card link does not match its ID.")
        thumbnail = None
        image = card.select_one(".serial__image")
        if image is not None:
            match = re.search(r"url\(\s*['\"]?(https://[^\s'\"()]+)['\"]?\s*\)",
                              image.get("style", ""))
            if match:
                thumbnail = match[1]
        item = NewSeries(slug, title.get_text(" ", strip=True), thumbnail)
        if slug in found and found[slug] != item:
            raise TrackerError("Conflicting new-series cards; history remains unchanged.")
        found[slug] = item
    return tuple(found.values())


def normalize_slug(value: str) -> str:
    if not isinstance(value, str):
        raise TrackerError("Series entries must be URL or slug strings.")
    if value.startswith("https://"):
        parsed = urlparse(value)
        match = re.fullmatch(r"/comic/ep_list/([A-Za-z0-9_-]+)/?", parsed.path)
        if parsed.netloc not in ("toptoon.com", "www.toptoon.com") or not match:
            raise TrackerError("Expected a Toptoon /comic/ep_list/<slug> URL.")
        value = match[1]
    if not SLUG.fullmatch(value):
        raise TrackerError("Invalid series slug; use the ID from its episode-list URL.")
    return value


def number(value: object) -> Decimal:
    if isinstance(value, bool) or not re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
        raise TrackerError("Chapter numbers must be nonnegative decimal numbers.")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise TrackerError("Invalid chapter number.") from exc
    return result


def load_config(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not 1 <= len(data) <= 20:
        raise TrackerError("series.json must contain between 1 and 20 URLs/slugs.")
    slugs = [normalize_slug(value) for value in data]
    if len(set(slugs)) != len(slugs):
        raise TrackerError("Duplicate series URLs/slugs in configuration.")
    return slugs


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "series": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("series"), dict):
        raise TrackerError("Invalid state schema; refusing to reset notification history.")
    for slug, entry in state["series"].items():
        normalize_slug(slug)
        if not isinstance(entry, dict) or "last_chapter" not in entry:
            raise TrackerError("Invalid state entry; refusing to reset history.")
        number(entry["last_chapter"])
    if "new_series" in state:
        seen = state["new_series"]
        if (not isinstance(seen, list) or not seen
                or any(not isinstance(slug, str) or not SLUG.fullmatch(slug) for slug in seen)
                or len(set(seen)) != len(seen)):
            raise TrackerError("Invalid new-series history; refusing to reset notifications.")
    return state


def save_state(path: Path, state: dict) -> None:
    """Atomic replacement: an interrupted write cannot truncate existing state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def release_value(text: str) -> tuple[str, str]:
    """Keep date-only precision; never pretend midnight is the release time."""
    text = text.strip()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", text):
        year, month, day = map(int, text.split("."))
        return date(2000 + year, month, day).isoformat(), "date"
    if re.fullmatch(r"\d{4}[.-]\d{2}[.-]\d{2}", text):
        return date.fromisoformat(text.replace(".", "-")).isoformat(), "date"
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.isoformat(), "timestamp"


def parse_series(html: str, slug: str, now: datetime | None = None) -> Series:
    """Only inspect the requested series' real episode rows, not recommendations."""
    now = now or datetime.now(KST)
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.select_one('meta[property="og:title"]')
    title = title_tag.get("content", "").strip() if title_tag else ""
    title = re.sub(r"^\s*\[탑툰\]\s*", "", title)
    image_tag = soup.select_one('meta[property="og:image"]')
    thumbnail = image_tag.get("content") if image_tag else None
    if thumbnail and urlparse(thumbnail).scheme != "https":
        thumbnail = None
    rows = [row for row in soup.select(".episode-items[data-comic-id][data-episode-id]")
            if row.get("data-comic-id") == slug]
    if not title or not rows:
        raise TrackerError("No matching public episode list: page changed or requires login/age verification.")
    episodes: dict[Decimal, Episode] = {}
    for row in rows:
        if {"disabled", "nonBtn", "eventFreeView"}.intersection(row.get("class", [])):
            continue
        label = row.select_one(".ep_title")
        if label is None:
            raise TrackerError("Episode title selector changed; refusing to advance state.")
        match = CHAPTER.match(label.get_text(" ", strip=True))
        if not match:
            LOG.warning("%s: ignoring nonstandard chapter label (special/prologue/season).", slug)
            continue
        chapter = number(match[1])
        episode_id = row.get("data-episode-id", "")
        if not SLUG.fullmatch(episode_id):
            raise TrackerError("Invalid episode ID in page.")
        date_tag = row.select_one(".ep_date .date")
        if date_tag is None:
            raise TrackerError("Release date selector changed; refusing to advance state.")
        try:
            release, precision = release_value(date_tag.get_text(" ", strip=True))
        except ValueError as exc:
            raise TrackerError("Unrecognized release date; refusing to advance state.") from exc
        if precision == "date":
            if date.fromisoformat(release) > now.astimezone(KST).date():
                continue
        elif datetime.fromisoformat(release) > now:
            continue
        episode = Episode(chapter, episode_id,
                          f"{BASE}/comic/ep_view/{quote(slug, safe='')}/{quote(episode_id, safe='')}",
                          release, precision)
        if chapter in episodes and episodes[chapter] != episode:
            raise TrackerError("Ambiguous duplicate chapter numbers; check season/special numbering.")
        episodes[chapter] = episode
    if not episodes:
        raise TrackerError("No released numeric chapters found; state remains unchanged.")
    return Series(slug, title, thumbnail, tuple(episodes[n] for n in sorted(episodes)))


class ToptoonClient:
    def __init__(self, interval: float = 5):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Referer": BASE + "/",
                                     "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                                     "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7"})
        self.interval = interval
        self.last_request = 0.0
        self.robots: RobotFileParser | None = None

    def close(self) -> None:
        self.session.close()

    def get(self, url: str) -> str:
        for attempt in range(3):
            wait = self.interval - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait + random.uniform(0, 1))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=(10, 30), allow_redirects=False)
            except requests.RequestException:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise TrackerError("Toptoon network error after retries.") from None
            if response.status_code in (401, 403, 429):
                # A rate-limit response ends this run; the next scheduled run is the retry.
                raise SiteBlocked(f"Toptoon HTTP {response.status_code}; stopping requests, no bypass attempted.")
            if 500 <= response.status_code < 600 and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            if response.status_code != 200:
                raise TrackerError(f"Toptoon HTTP {response.status_code}; redirects/access gates are not followed.")
            response.encoding = "utf-8"
            return response.text
        raise TrackerError("Toptoon retries exhausted.")

    def fetch_page(self, url: str) -> str:
        if self.robots is None:
            self.robots = RobotFileParser()
            self.robots.parse(self.get(BASE + "/robots.txt").splitlines())
            delay = self.robots.crawl_delay(USER_AGENT)
            rate = self.robots.request_rate(USER_AGENT)
            self.interval = max(self.interval, float(delay or 0), rate.seconds / rate.requests if rate else 0)
        if not self.robots.can_fetch(USER_AGENT, url):
            raise SiteBlocked("robots.txt disallows polling this path.")
        return self.get(url)

    def fetch(self, slug: str) -> Series:
        return parse_series(self.fetch_page(f"{BASE}/comic/ep_list/{slug}"), slug)

    def fetch_new_series(self) -> tuple[NewSeries, ...]:
        # URL fragments are browser-only; complete1 is selected in the returned HTML.
        return parse_new_series(self.fetch_page(BASE + "/complete"))


def webhook_url() -> str:
    value = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    parsed = urlparse(value)
    if (parsed.scheme != "https" or parsed.netloc != "discord.com"
            or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+", parsed.path)
            or parsed.query or parsed.fragment):
        raise TrackerError("Set DISCORD_WEBHOOK_URL to a Discord webhook URL (never put it in config/state).")
    return value


def send_discord(url: str, payload: dict) -> None:
    """Retry explicit 429 rejection only; ambiguous POST failures may have delivered."""
    for attempt in range(3):
        try:
            response = requests.post(url, params={"wait": "true"}, json=payload,
                                     timeout=(10, 30), allow_redirects=False)
        except requests.RequestException:
            raise TrackerError("Discord network error; delivery uncertain, chapter state not advanced.") from None
        if response.status_code == 429 and attempt < 2:
            try:
                delay = float(response.json()["retry_after"])
            except (ValueError, KeyError, TypeError):
                delay = 5
            if not 0 <= delay <= 120:
                raise TrackerError("Discord rate limit exceeds retry budget; state not advanced.")
            time.sleep(delay + 1)
            continue
        if response.status_code not in (200, 204):
            raise TrackerError(f"Discord HTTP {response.status_code}; state not advanced.")
        return
    raise TrackerError("Discord retries exhausted.")


def make_payload(series: Series, episode: Episode) -> dict:
    detected = datetime.now(timezone.utc).isoformat()
    release = episode.release
    if episode.precision == "date":
        release += " (Korea; exact time not published in episode list)"
    embed = {"title": series.title[:256], "url": episode.url, "color": 0xF45236,
             "description": f"New chapter **{episode.number}** is available.",
             "fields": [{"name": "Chapter", "value": str(episode.number), "inline": True},
                        {"name": "Release", "value": release},
                        {"name": "Read chapter", "value": f"[Open on Toptoon]({episode.url})"}],
             "footer": {"text": "Toptoon tracker • Detection time"}, "timestamp": detected}
    if series.thumbnail:
        embed["thumbnail"] = {"url": series.thumbnail}
    return {"username": "Toptoon Release Tracker", "allowed_mentions": {"parse": []}, "embeds": [embed]}


def make_new_series_payload(series: NewSeries) -> dict:
    embed = {"title": series.title[:256], "url": series.url, "color": 0xF45236,
             "description": f"New series spotted on Toptoon!\n[Read series]({series.url})",
             "footer": {"text": "Toptoon new-series tracker • Detection time"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    if series.thumbnail:
        embed["thumbnail"] = {"url": series.thumbnail}
    return {"username": "Toptoon Release Tracker", "allowed_mentions": {"parse": []},
            "embeds": [embed]}


def process_new_series(items: tuple[NewSeries, ...], state: dict, path: Path,
                       webhook: str | None, dry_run: bool = False) -> None:
    if not items:
        raise TrackerError("Empty new-series list; history remains unchanged.")
    if "new_series" not in state:
        if dry_run:
            LOG.info("Would baseline %d existing series (no alerts).", len(items))
            return
        state["new_series"] = sorted({item.slug for item in items})
        save_state(path, state)
        LOG.info("Baselined %d existing series (no historical alerts).", len(items))
        return
    seen = set(state["new_series"])
    pending = [item for item in items if item.slug not in seen]
    LOG.info("New-series tab: listed=%d, unseen=%d", len(items), len(pending))
    for item in pending:
        if item.slug in seen:
            continue
        if dry_run:
            LOG.info("Would notify new series: %s, link=%s", item.slug, item.url)
            continue
        if webhook is None:
            raise TrackerError("Webhook is not configured.")
        send_discord(webhook, make_new_series_payload(item))
        seen.add(item.slug)
        # Keep all seen IDs even after titles leave the rotating new-series tab.
        state["new_series"] = sorted(seen)
        save_state(path, state)
        LOG.info("Notified new series: %s", item.slug)
        time.sleep(1)


def process_series(series: Series, state: dict, path: Path, webhook: str | None,
                   dry_run: bool = False) -> None:
    entry = state["series"].get(series.slug)
    latest = series.episodes[-1]
    if entry is None:
        if dry_run:
            LOG.info("%s: would initialize baseline at chapter %s (no alert).", series.slug, latest.number)
            return
        state["series"][series.slug] = {"last_chapter": str(latest.number), "episode_id": latest.episode_id}
        save_state(path, state)
        LOG.info("%s: baseline set to chapter %s (no historical alerts).", series.slug, latest.number)
        return
    previous = number(entry["last_chapter"])
    if latest.number < previous:
        raise TrackerError("Latest chapter regressed; refusing to lower saved high-water mark.")
    pending = [ep for ep in series.episodes if ep.number > previous]
    LOG.info("%s: latest=%s, saved=%s, new=%d", series.slug, latest.number, previous, len(pending))
    for episode in pending:
        if dry_run:
            LOG.info("Would notify: %s chapter %s, release=%s, link=%s",
                     series.slug, episode.number, episode.release, episode.url)
            continue
        if webhook is None:
            raise TrackerError("Webhook is not configured.")
        send_discord(webhook, make_payload(series, episode))
        # Never advance before Discord acknowledges. Save each delivered chapter individually.
        state["series"][series.slug] = {"last_chapter": str(episode.number), "episode_id": episode.episode_id}
        save_state(path, state)
        LOG.info("%s: notified chapter %s", series.slug, episode.number)
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("series.json"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--new-series-only", action="store_true", help="Skip configured chapter checks")
    parser.add_argument("--dry-run", action="store_true", help="Read Toptoon without sending or changing state")
    parser.add_argument("--test-notification", action="store_true", help="Send one test without polling or changing state")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = ToptoonClient()
    failed = False
    try:
        if args.dry_run and args.test_notification:
            raise TrackerError("Choose dry-run or test-notification, not both.")
        webhook = None if args.dry_run else webhook_url()
        if args.test_notification:
            send_discord(webhook, {"content": "Toptoon tracker test: Discord delivery works.",
                                   "allowed_mentions": {"parse": []}})
            LOG.info("Test notification sent.")
            return 0
        state = load_state(args.state)
        try:
            process_new_series(client.fetch_new_series(), state, args.state, webhook, args.dry_run)
        except SiteBlocked:
            raise
        except TrackerError as exc:
            LOG.error("New series: %s", exc)
            failed = True
        slugs = [] if args.new_series_only else load_config(args.config)
        for slug in slugs:
            try:
                process_series(client.fetch(slug), state, args.state, webhook, args.dry_run)
            except SiteBlocked as exc:
                LOG.error("%s: %s", slug, exc)
                failed = True
                break
            except TrackerError as exc:
                LOG.error("%s: %s", slug, exc)
                failed = True
    except TrackerError as exc:
        LOG.error("%s", exc)
        return 1
    except (OSError, ValueError):
        # Raw exception strings may contain sensitive configuration or response data.
        LOG.error("Configuration/state/IO error. Check JSON schemas and paths; existing history is not reset.")
        return 1
    finally:
        client.close()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
