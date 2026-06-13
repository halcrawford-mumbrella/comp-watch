"""
Comp Watch — competitor headline aggregator for Mumbrella
Local:      python3 app.py  →  http://localhost:8766
Production: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
"""
import os
import re
import threading
import time
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from flask import Flask, jsonify, render_template

app = Flask(__name__, template_folder="templates")

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ── Source definitions ─────────────────────────────────────────────────────────

RSS_SOURCES = [
    {"name": "B&T",           "url": "https://www.bandt.com.au/feed/"},
    {"name": "Campaign Brief","url": "https://campaignbrief.com/feed/"},
    {"name": "Mediaweek",     "url": "https://www.mediaweek.com.au/feed/"},
    {"name": "Mi3",           "url": "https://news.google.com/rss/search?q=site:mi-3.com.au&hl=en-AU&gl=AU&ceid=AU:en",
                              "strip_suffix": " - Mi-3.com.au"},
    # LBBOnline is Cloudflare-protected with cloud-IP blocking — Google News with AU
    # location terms gives the Australia edition content without needing direct access.
    {"name": "LBBOnline",     "url": "https://news.google.com/rss/search?q=site:lbbonline.com+(australia+OR+%22new+zealand%22+OR+sydney+OR+melbourne+OR+brisbane+OR+auckland)&hl=en-AU&gl=AU&ceid=AU:en",
                              "strip_suffix": " - Little Black Book | LBBOnline"},
]

# Sites that need a real browser (Cloudflare JS challenge, heavy SPA rendering, etc.)
# Note: Cloudflare sites that also block cloud/datacenter IPs cannot be reached from
# Railway even with a patched browser — a residential proxy would be needed for those.
PLAYWRIGHT_SOURCES: list = []

# ── Helpers ────────────────────────────────────────────────────────────────────

_STALE_TS = re.compile(r'\s*\d+\s+(?:day|hour|minute|second)s?\s+ago\s*$', re.I)

_cache: list = []
_lock = threading.Lock()
_last_updated: str | None = None


def _parse_entry_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _parse_date_str(s: str) -> datetime | None:
    try:
        dt = dateparser.parse(s)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_rss(source: dict) -> list:
    try:
        resp = requests.get(
            source["url"],
            headers={"User-Agent": CHROME_UA},
            timeout=15,
        )
        feed = feedparser.parse(resp.text)
        suffix = source.get("strip_suffix", "")
        items = []
        for entry in feed.entries[:25]:
            title = _STALE_TS.sub("", entry.get("title", "")).strip()
            if suffix and title.endswith(suffix):
                title = title[: -len(suffix)].strip()
            link = entry.get("link", "").strip()
            if not title or not link:
                continue
            pub = _parse_entry_date(entry)
            items.append({
                "title": title,
                "url": link,
                "source": source["name"],
                "published": pub.isoformat() if pub else None,
            })
        return items
    except Exception as e:
        print(f"[{source['name']}] RSS error: {e}")
        return []


def fetch_adnews() -> list:
    try:
        resp = requests.get(
            "https://www.adnews.com.au/news",
            headers={"User-Agent": CHROME_UA},
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "lxml")
        items = []
        seen: set = set()
        for a in soup.find_all("a", href=re.compile(r'/(?:news|article)/[^/]+')):
            title = a.get_text(strip=True)
            if not title or len(title) < 20 or title in seen:
                continue
            seen.add(title)
            href = a["href"]
            url = href if href.startswith("http") else f"https://www.adnews.com.au{href}"
            pub = None
            parent = a.find_parent(["article", "div", "li", "section"])
            if parent:
                t = parent.find("time")
                if t:
                    pub = _parse_date_str(t.get("datetime") or t.get_text(strip=True))
            items.append({
                "title": title,
                "url": url,
                "source": "AdNews",
                "published": pub.isoformat() if pub else None,
            })
            if len(items) >= 20:
                break
        return items
    except Exception as e:
        print(f"[AdNews] scrape error: {e}")
        return []


def fetch_playwright(source: dict) -> list:
    """Generic Playwright fetcher for JS-rendered / Cloudflare-protected sites."""
    try:
        from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print(f"[{source['name']}] patchright not installed")
        return []

    items = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            ctx = browser.new_context(
                user_agent=CHROME_UA,
                locale="en-AU",
                timezone_id="Australia/Sydney",
            )
            page = ctx.new_page()
            page.goto(source["url"], wait_until="networkidle", timeout=30_000)

            # Wait for any article-like element to appear
            for sel in source.get("article_sel", ["article"]):
                try:
                    page.wait_for_selector(sel, timeout=8_000)
                    break
                except PWTimeout:
                    continue

            # Find article containers
            containers = []
            for sel in source.get("article_sel", ["article"]):
                containers = page.query_selector_all(sel)
                if containers:
                    break

            seen: set = set()
            for container in containers[:25]:
                # Title
                title = ""
                for sel in source.get("title_sel", ["h2", "h3"]):
                    el = container.query_selector(sel)
                    if el:
                        title = el.inner_text().strip()
                        if title:
                            break

                # Link
                href = ""
                for sel in source.get("link_sel", ["a[href]"]):
                    el = container.query_selector(sel)
                    if el:
                        href = el.get_attribute("href") or ""
                        if href:
                            break

                if not title or not href or title in seen:
                    continue
                seen.add(title)

                url = href if href.startswith("http") else f"https://{source['url'].split('/')[2]}{href}"

                # Date
                pub = None
                for sel in source.get("time_sel", ["time"]):
                    el = container.query_selector(sel)
                    if el:
                        dt_str = el.get_attribute("datetime") or el.inner_text().strip()
                        pub = _parse_date_str(dt_str)
                        if pub:
                            break

                items.append({
                    "title": title,
                    "url": url,
                    "source": source["name"],
                    "published": pub.isoformat() if pub else None,
                })

            browser.close()
            print(f"[{source['name']}] playwright fetched {len(items)} items")
    except Exception as e:
        print(f"[{source['name']}] playwright error: {e}")

    return items


# ── Refresh loop ───────────────────────────────────────────────────────────────

def refresh_all():
    global _last_updated
    raw = []

    for src in RSS_SOURCES:
        raw.extend(fetch_rss(src))
    raw.extend(fetch_adnews())
    for src in PLAYWRIGHT_SOURCES:
        raw.extend(fetch_playwright(src))

    seen_urls: set = set()
    deduped = []
    for item in raw:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            deduped.append(item)

    deduped.sort(key=lambda x: x["published"] or "0000", reverse=True)

    with _lock:
        _cache[:] = deduped
        _last_updated = datetime.now(timezone.utc).isoformat()

    print(f"[refresh] {len(deduped)} headlines total")


def _background():
    while True:
        try:
            refresh_all()
        except Exception as e:
            print(f"[refresh] error: {e}")
        time.sleep(300)


threading.Thread(target=_background, daemon=True).start()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/headlines")
def headlines():
    with _lock:
        return jsonify({"items": list(_cache), "last_updated": _last_updated})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8766))
    app.run(port=port, debug=True, use_reloader=False)
