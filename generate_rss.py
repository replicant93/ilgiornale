import json
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.ilgiornale.it"
SOURCES = [
    BASE + "/",
    BASE + "/politica/",
    BASE + "/interni/",
    BASE + "/economia/",
    BASE + "/cronache/attualita/",
    BASE + "/mondo/",
    BASE + "/sport/",
    BASE + "/cultura/",
    BASE + "/rubriche/",
]
FEED_FILE = Path("feed.xml")
DB_FILE = Path("articles.json")
MAX_ITEMS = 100

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IlGiornaleRSS/2.0; +https://github.com/replicant93/ilgiornale)",
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}

MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

def clean(text):
    return re.sub(r"\s+", " ", unescape(text or "")).strip()

def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def fetch(url):
    # Query parameter prevents an intermediary cache from serving an old page.
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}rss_refresh={int(time.time())}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text

def parse_date(text):
    text = clean(text).lower()
    # 29 08 2026
    m = re.search(r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{4})\b", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
        except ValueError:
            pass
    # 29 agosto 2026 - 12:34
    m = re.search(
        r"\b(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
        r"settembre|ottobre|novembre|dicembre)\s+(\d{4})(?:\s*[-–]\s*(\d{1,2}):(\d{2}))?",
        text,
    )
    if m:
        try:
            hour = int(m.group(4) or 0)
            minute = int(m.group(5) or 0)
            return datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)),
                            hour, minute, tzinfo=timezone.utc)
        except ValueError:
            pass
    return None

def is_article_url(url):
    p = urlparse(url)
    if p.netloc not in ("www.ilgiornale.it", "ilgiornale.it"):
        return False
    path = p.path.rstrip("/")
    if not path or path.count("/") < 2:
        return False
    blocked = ("/search", "/tag/", "/autore/", "/rubriche/", "/video/", "/podcast/")
    if any(path.startswith(x) for x in blocked):
        return False
    # Real articles on the current site are under /news/... or section/article slugs.
    if path.endswith(("/politica", "/economia", "/interni", "/mondo", "/sport", "/cultura")):
        return False
    if re.search(r"/\d+/?$", path):  # pagination
        return False
    return True

def parse_source(html):
    soup = BeautifulSoup(html, "html.parser")
    found = {}

    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"])
        if not is_article_url(url):
            continue

        title = clean(a.get_text(" ", strip=True))
        if not (20 <= len(title) <= 260):
            continue

        # Look at a nearby article/card container for summary, author and date.
        node = a
        best_text = ""
        for _ in range(5):
            if node.parent is None:
                break
            node = node.parent
            txt = clean(node.get_text(" ", strip=True))
            if 50 <= len(txt) <= 3500:
                best_text = txt
                # Stop at a useful card/article-sized block.
                if node.name in ("article", "li", "div"):
                    break

        date = parse_date(best_text)
        if date is None:
            # Also inspect semantic time elements near the link.
            container = node
            t = container.find("time") if container else None
            if t:
                date = parse_date(t.get("datetime", "")) or parse_date(t.get_text(" ", strip=True))

        img_url = ""
        img = a.find("img") or (node.find("img") if node else None)
        if img:
            img_url = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            img_url = urljoin(BASE, img_url)

        # Avoid navigation cards that have no real article date.
        if date is None:
            date = datetime.now(timezone.utc)

        summary = best_text
        if summary.startswith(title):
            summary = summary[len(title):].strip()
        summary = summary[:600] or title

        item = {
            "title": title,
            "url": url,
            "summary": summary,
            "image": img_url,
            "pubdate": date.isoformat(),
        }

        # Keep the newest observation for a URL.
        if url not in found or date > datetime.fromisoformat(found[url]["pubdate"]):
            found[url] = item

    return list(found.values())

def load_db():
    if not DB_FILE.exists():
        return []
    try:
        data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_db(items):
    DB_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def make_rss(items):
    now = datetime.now(timezone.utc)
    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        "<title>Il Giornale - Ultime notizie</title>",
        f"<link>{BASE}/</link>",
        "<description>Feed RSS personale delle ultime notizie pubblicate da il Giornale</description>",
        '<language>it-IT</language>',
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
        f'<atom:link href="https://replicant93.github.io/ilgiornale/feed.xml" '
        'rel="self" type="application/rss+xml" />',
    ]

    for item in items[:MAX_ITEMS]:
        dt = datetime.fromisoformat(item["pubdate"])
        chunks.extend([
            "<item>",
            f"<title>{esc(item['title'])}</title>",
            f"<link>{esc(item['url'])}</link>",
            f"<guid isPermaLink=\"true\">{esc(item['url'])}</guid>",
            f"<pubDate>{format_datetime(dt)}</pubDate>",
            f"<description>{esc(item.get('summary', ''))}</description>",
        ])
        if item.get("image"):
            chunks.append(
                f'<media:content url="{esc(item["image"])}" medium="image" />'
            )
        chunks.append("</item>")

    chunks.extend(["</channel>", "</rss>"])
    FEED_FILE.write_text("\n".join(chunks), encoding="utf-8")

def main():
    all_found = {}

    for source in SOURCES:
        try:
            html = fetch(source)
            for item in parse_source(html):
                old = all_found.get(item["url"])
                if old is None or item["pubdate"] > old["pubdate"]:
                    all_found[item["url"]] = item
            print(f"OK: {source}")
        except Exception as exc:
            print(f"WARN: {source}: {exc}")

    old_items = load_db()

    # Merge old + new by URL, with new data taking precedence.
    merged = {x["url"]: x for x in old_items if isinstance(x, dict) and x.get("url")}
    merged.update(all_found)

    # Newest first. This is the key fix: never preserve the old file order.
    items = list(merged.values())
    items.sort(key=lambda x: x.get("pubdate", ""), reverse=True)
    items = items[:MAX_ITEMS]

    save_db(items)
    make_rss(items)

    newest = items[0]["title"] if items else "nessun articolo"
    newest_date = items[0]["pubdate"] if items else "-"
    print(f"Articoli trovati ora: {len(all_found)}")
    print(f"Articoli nel feed: {len(items)}")
    print(f"Più recente: {newest} ({newest_date})")

if __name__ == "__main__":
    main()
