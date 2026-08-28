import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.ilgiornale.it"
HOME = BASE + "/"
FEED_FILE = Path("feed.xml")
DB_FILE = Path("articles.json")
MAX_ITEMS = 100
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IlGiornaleRSS/1.0; +https://github.com/replicant93/ilgiornale)"
}

def clean(text):
    return re.sub(r"\s+", " ", unescape(text or "")).strip()

def get(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text

def parse_articles(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(BASE, a["href"])
        if not href.startswith(BASE + "/"):
            continue
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 20 or len(title) > 250:
            continue
        # Avoid navigation/category/search URLs; article URLs generally have a slug.
        path = href[len(BASE):]
        if path.count("/") < 2 or any(x in path for x in ["/search", "/tag/", "/author/"]):
            continue
        # Prefer links whose surrounding block looks like an article.
        parent = a
        for _ in range(3):
            if parent.parent:
                parent = parent.parent
        text = clean(parent.get_text(" ", strip=True))
        if len(text) > 5000:
            text = text[:5000]
        image = ""
        img = a.find("img") or parent.find("img")
        if img:
            image = img.get("src") or img.get("data-src") or ""
            image = urljoin(BASE, image)
        out[href] = {"title": title, "url": href, "summary": text[:500] if text else title, "image": image}
    return list(out.values())

def load_db():
    if not DB_FILE.exists():
        return []
    try:
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_db(items):
    DB_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

def make_rss(items):
    now = datetime.now(timezone.utc)
    chunks = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        "<title>Il Giornale - Ultime notizie</title>",
        f"<link>{BASE}/</link>",
        "<description>Feed RSS ricostruito delle ultime notizie pubblicate da Il Giornale</description>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
    ]
    for item in items[:MAX_ITEMS]:
        def esc(s):
            return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
        chunks += [
            "<item>",
            f"<title>{esc(item['title'])}</title>",
            f"<link>{esc(item['url'])}</link>",
            f"<guid isPermaLink=\"true\">{esc(item['url'])}</guid>",
            f"<description>{esc(item.get('summary',''))}</description>",
        ]
        if item.get("image"):
            chunks.append(f"<media:content url=\"{esc(item['image'])}\" medium=\"image\" />")
        chunks += ["</item>"]
    chunks += ["</channel>", "</rss>"]
    FEED_FILE.write_text("\n".join(chunks), encoding="utf-8")

def main():
    html = get(HOME)
    found = parse_articles(html)
    old = load_db()
    by_url = {x["url"]: x for x in old}
    # New entries first; preserve previous entries afterward.
    merged = []
    for x in found + old:
        if x["url"] not in {y["url"] for y in merged}:
            merged.append(x)
    merged = merged[:MAX_ITEMS]
    save_db(merged)
    make_rss(merged)
    print(f"Found {len(found)} candidate articles; feed contains {len(merged)} items.")

if __name__ == "__main__":
    main()
