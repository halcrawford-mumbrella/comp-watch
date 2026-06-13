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

RSS_SOURCES = [
    {"name": "B&T",           "url": "https://www.bandt.com.au/feed/"},
    {"name": "Campaign Brief","url": "https://campaignbrief.com/feed/"},
    {"name": "Mediaweek",     "url": "https://www.mediaweek.com.au/feed/"},
    {"name": "Mi3",           "url": "https://news.google.com/rss/search?q=site:mi-3.com.au&hl=en-AU&gl=AU&ceid=AU:en",
                              "strip_suffix": " - Mi-3.com.au"},
    {"name": "LBBOnline",     "url": "https://news.google.com/rss/search?q=site:lbbonline.com&hl=en-AU&gl=AU&ceid=AU:en",
                              "strip_suffix": " - Little Black Book | LBBOnline"},
]

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


def fetch_rss(source: dict) -> list:
    try:
        resp = requests.get(
            source["url"],
            headers={"User-Agent": CHROME_UA},
            timeout=15,
        )
        feed = feedparser.parse(resp.text)
        items = []
        suffix = source.get("strip_suffix", "")
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
        seen = set()

        # Cast a wide net for article links
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
                    dt_str = t.get("datetime") or t.get_text(strip=True)
                    try:
                        pub = dateparser.parse(dt_str)
                        if pub and pub.tzinfo is None:
                            pub = pub.replace(tzinfo=timezone.utc)
                    except Exception:
                        pass

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


def refresh_all():
    global _last_updated
    raw = []
    for src in RSS_SOURCES:
        raw.extend(fetch_rss(src))
    raw.extend(fetch_adnews())

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

    print(f"[refresh] {len(deduped)} headlines")


def _background():
    while True:
        try:
            refresh_all()
        except Exception as e:
            print(f"[refresh] error: {e}")
        time.sleep(300)


threading.Thread(target=_background, daemon=True).start()


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
