import json
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.ilgiornale.it"
HOME = BASE + "/"
FEED_FILE = Path("feed.xml")
DB_FILE = Path("articles.json")
MAX_ITEMS = 100
MAX_NEW_ARTICLES_PER_RUN = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; IlGiornaleRSS/3.0; +https://github.com/replicant93/ilgiornale)",
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}

MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

session = requests.Session()
session.headers.update(HEADERS)

def clean(text):
    return re.sub(r"\s+", " ", unescape(text or "")).strip()

def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def fetch(url):
    # Cache-busting query. The server may ignore it, but it prevents simple
    # intermediary caches from reusing an older response.
    sep = "&" if "?" in url else "?"
    return session.get(f"{url}{sep}rss_refresh={time.time_ns()}", timeout=30)

def is_article_url(url):
    p = urlparse(url)
    if p.netloc not in ("www.ilgiornale.it", "ilgiornale.it"):
        return False
    path = p.path.rstrip("/")
    if not path or path.count("/") < 2:
        return False
    # Current ilGiornale article URLs are generally /news/.../.../
    # Keep other article-like URLs but reject obvious non-articles.
    blocked = (
        "/search", "/tag/", "/autore/", "/video/", "/podcast/",
        "/newsletter", "/abbonati", "/login"
    )
    if any(path.startswith(x) for x in blocked):
        return False
    if re.search(r"/\d+/?$", path):
        return False
    return True

def extract_article_links(html):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"])
        if not is_article_url(url):
            continue
        title = clean(a.get_text(" ", strip=True))
        if not (20 <= len(title) <= 260):
            continue

        # Find a nearby card for a useful excerpt/image, but DO NOT parse dates
        # from arbitrary surrounding text (that was the source of the old bug).
        node = a
        card_text = ""
        for _ in range(5):
            if node.parent is None:
                break
            node = node.parent
            txt = clean(node.get_text(" ", strip=True))
            if 50 <= len(txt) <= 3500:
                card_text = txt
                if node.name in ("article", "li"):
                    break

        summary = card_text
        if summary.startswith(title):
            summary = summary[len(title):].strip()
        summary = summary[:600] or title

        image = ""
        img = a.find("img") or (node.find("img") if node else None)
        if img:
            image = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or ""
            )
            image = urljoin(BASE, image)

        found[url] = {
            "title": title,
            "url": url,
            "summary": summary,
            "image": image,
        }
    return list(found.values())

def parse_iso(value):
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def parse_human_date(value):
    value = clean(value).lower()
    m = re.search(
        r"\b(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
        r"settembre|ottobre|novembre|dicembre)\s+(\d{4})(?:\s*[-–]\s*(\d{1,2}):(\d{2}))?",
        value,
    )
    if not m:
        return None
    try:
        return datetime(
            int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)),
            int(m.group(4) or 0), int(m.group(5) or 0),
            tzinfo=timezone.utc
        )
    except Exception:
        return None

def article_published_date(url):
    """Get the article's own publication date.

    IMPORTANT: never use arbitrary text from a card/article body as a date.
    It can contain dates about historical events or future appointments.
    """
    try:
        r = fetch(url)
        r.raise_for_status()
    except Exception as exc:
        print(f"WARN date {url}: {exc}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # 1) JSON-LD: preferred on modern news sites.
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        # Also handle @graph.
        expanded = []
        for node in nodes:
            if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                expanded.extend(node["@graph"])
            else:
                expanded.append(node)
        for node in expanded:
            if not isinstance(node, dict):
                continue
            typ = node.get("@type", "")
            types = typ if isinstance(typ, list) else [typ]
            if any(str(t).lower() in ("newsarticle", "article") for t in types):
                for key in ("datePublished", "dateCreated"):
                    dt = parse_iso(node.get(key))
                    if dt:
                        return dt

    # 2) Standard OpenGraph/article metadata.
    for prop in ("article:published_time", "article:published", "og:published_time"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag:
            dt = parse_iso(tag.get("content"))
            if dt:
                return dt

    # 3) <time datetime="..."> near the article.
    for tag in soup.find_all("time"):
        dt = parse_iso(tag.get("datetime"))
        if dt:
            return dt
        dt = parse_human_date(tag.get_text(" ", strip=True))
        if dt:
            return dt

    # 4) Only as a last resort, use the site's visible article date if it has
    # the explicit "giorno mese anno - hh:mm" form.
    for text in soup.stripped_strings:
        dt = parse_human_date(text)
        if dt:
            return dt

    return None

def load_db():
    if not DB_FILE.exists():
        return []
    try:
        data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_db(items):
    DB_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

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
        "<language>it-IT</language>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
        '<atom:link href="https://replicant93.github.io/ilgiornale/feed.xml" '
        'rel="self" type="application/rss+xml" />',
    ]

    for item in items[:MAX_ITEMS]:
        dt = parse_iso(item.get("pubdate")) or now
        chunks.extend([
            "<item>",
            f"<title>{esc(item.get('title'))}</title>",
            f"<link>{esc(item.get('url'))}</link>",
            f"<guid isPermaLink=\"true\">{esc(item.get('url'))}</guid>",
            f"<pubDate>{format_datetime(dt)}</pubDate>",
            f"<description>{esc(item.get('summary'))}</description>",
        ])
        if item.get("image"):
            chunks.append(
                f'<media:content url="{esc(item["image"])}" medium="image" />'
            )
        chunks.append("</item>")

    chunks.extend(["</channel>", "</rss>"])
    FEED_FILE.write_text("\n".join(chunks), encoding="utf-8")

def main():
    now = datetime.now(timezone.utc)
    old_items = load_db()
    old_by_url = {
        x["url"]: x for x in old_items
        if isinstance(x, dict) and x.get("url")
    }

    # Fetch ONLY the homepage. The previous version requested several section
    # URLs that now return 404, and it also parsed dates from unrelated text.
    r = fetch(HOME)
    r.raise_for_status()
    candidates = extract_article_links(r.text)

    new_urls = [x["url"] for x in candidates if x["url"] not in old_by_url]
    # Newest stories are normally near the top of the homepage.
    new_urls = new_urls[:MAX_NEW_ARTICLES_PER_RUN]

    print(f"Link candidati in homepage: {len(candidates)}")
    print(f"Nuovi URL rispetto al database: {len(new_urls)}")

    # Add/update only genuinely new URLs.
    for candidate in candidates:
        if candidate["url"] not in old_by_url:
            if candidate["url"] not in new_urls:
                continue
            dt = article_published_date(candidate["url"])
            if dt is None:
                # Do not invent a historical date. Detection time is used only
                # when the article itself exposes no date at all.
                dt = now
            candidate["pubdate"] = dt.isoformat()
            old_by_url[candidate["url"]] = candidate

    # One-time repair: the old scraper could assign dates found in article
    # text (e.g. "19 ottobre") to unrelated stories. If such dates are in the
    # future, re-read the affected article's own metadata.
    repaired = 0
    future_limit = now + timedelta(hours=2)
    for item in list(old_by_url.values()):
        dt = parse_iso(item.get("pubdate"))
        if dt and dt > future_limit:
            real_dt = article_published_date(item["url"])
            if real_dt:
                item["pubdate"] = real_dt.isoformat()
                repaired += 1

    items = list(old_by_url.values())
    items.sort(
        key=lambda x: parse_iso(x.get("pubdate")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    items = items[:MAX_ITEMS]

    save_db(items)
    make_rss(items)

    newest = items[0] if items else {}
    print(f"Articoli nel feed: {len(items)}")
    print(f"Date future riparate: {repaired}")
    print(f"Più recente: {newest.get('title', 'nessuno')}")
    print(f"Data più recente: {newest.get('pubdate', '-')}")

if __name__ == "__main__":
    main()
