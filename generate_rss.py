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
FEED_URL = "https://replicant93.github.io/ilgiornale/feed.xml"
DB_FILE = Path("articles.json")
FEED_FILE = Path("feed.xml")

MAX_ITEMS = 100
MAX_NEW_PER_RUN = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 "
        "IlGiornaleRSS/4.0"
    ),
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


def clean(value):
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def esc(value):
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def fetch(url):
    sep = "&" if "?" in url else "?"
    return session.get(
        f"{url}{sep}rss_refresh={time.time_ns()}",
        timeout=30,
    )


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_italian_date(value):
    """Parse only an explicit publication-style date.

    Accepted form:
        30 marzo 2026 - 11:18
        30 marzo 2026
    """
    value = clean(value).lower()

    m = re.search(
        r"\b(\d{1,2})\s+"
        r"(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
        r"settembre|ottobre|novembre|dicembre)\s+"
        r"(\d{4})"
        r"(?:\s*[-–]\s*(\d{1,2}):(\d{2}))?\b",
        value,
    )
    if not m:
        return None

    try:
        return datetime(
            int(m.group(3)),
            MONTHS[m.group(2)],
            int(m.group(1)),
            int(m.group(4) or 0),
            int(m.group(5) or 0),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def is_article_url(url):
    parsed = urlparse(url)
    if parsed.netloc not in ("www.ilgiornale.it", "ilgiornale.it"):
        return False

    path = parsed.path.rstrip("/")
    if not path or path.count("/") < 2:
        return False

    blocked = (
        "/search",
        "/tag/",
        "/autore/",
        "/video/",
        "/podcast/",
        "/newsletter",
        "/login",
        "/abbonati",
    )
    if any(path.startswith(prefix) for prefix in blocked):
        return False

    if re.search(r"/\d+/?$", path):
        return False

    return True


def homepage_candidates():
    response = fetch(BASE + "/")
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    found = {}

    for a in soup.find_all("a", href=True):
        url = urljoin(BASE, a["href"])
        if not is_article_url(url):
            continue

        title = clean(a.get_text(" ", strip=True))
        if not 20 <= len(title) <= 260:
            continue

        node = a
        card_text = ""
        image = ""

        for _ in range(5):
            if node.parent is None:
                break
            node = node.parent
            text = clean(node.get_text(" ", strip=True))

            if 50 <= len(text) <= 3500:
                card_text = text

            img = node.find("img")
            if img and not image:
                image = (
                    img.get("src")
                    or img.get("data-src")
                    or img.get("data-lazy-src")
                    or ""
                )

            if node.name in ("article", "li"):
                break

        image = urljoin(BASE, image) if image else ""

        summary = card_text
        if summary.startswith(title):
            summary = summary[len(title):].strip()
        summary = summary[:600] or title

        found[url] = {
            "title": title,
            "url": url,
            "summary": summary,
            "image": image,
        }

    return list(found.values())


def jsonld_publication_date(soup):
    """Extract datePublished/dateCreated from JSON-LD."""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]

        while stack:
            obj = stack.pop()

            if isinstance(obj, list):
                stack.extend(obj)
                continue

            if not isinstance(obj, dict):
                continue

            graph = obj.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)

            typ = obj.get("@type", [])
            types = typ if isinstance(typ, list) else [typ]
            types = {str(x).lower() for x in types}

            if "newsarticle" in types or "article" in types:
                for key in ("datePublished", "dateCreated"):
                    dt = parse_iso(obj.get(key))
                    if dt:
                        return dt

    return None


def article_publication_date(url):
    """Get the publication date from the article page only.

    We NEVER search arbitrary article-body text for a date. That was the bug
    that turned 'dal 16 al 19 ottobre 2026' into a fake publication date.
    """
    try:
        response = fetch(url)
        response.raise_for_status()
    except Exception as exc:
        print(f"WARN: impossibile leggere {url}: {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # 1. JSON-LD (preferred)
    dt = jsonld_publication_date(soup)
    if dt:
        return dt

    # 2. Explicit publication meta tags
    for attr, key in (
        ("property", "article:published_time"),
        ("property", "article:published"),
        ("name", "article:published_time"),
        ("name", "date"),
        ("name", "pubdate"),
    ):
        tag = soup.find("meta", attrs={attr: key})
        if tag:
            dt = parse_iso(tag.get("content"))
            if dt:
                return dt
            dt = parse_italian_date(tag.get("content", ""))
            if dt:
                return dt

    # 3. <time> elements with datetime
    for tag in soup.find_all("time"):
        dt = parse_iso(tag.get("datetime"))
        if dt:
            return dt

    # 4. Find the title, then inspect ONLY the short block immediately
    #    following the headline. On ilGiornale this contains:
    #       subtitle / author / "30 marzo 2026 - 11:18"
    h1 = soup.find("h1")
    if h1:
        # First look at the next ~15 textual elements, not the whole article.
        current = h1
        checked = 0
        while current is not None and checked < 20:
            current = current.find_next()
            if current is None:
                break
            checked += 1

            text = clean(current.get_text(" ", strip=True))
            if not text or len(text) > 500:
                continue

            # Strong guard: require a publication-style date with a time.
            match = re.search(
                r"\b\d{1,2}\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|"
                r"luglio|agosto|settembre|ottobre|novembre|dicembre)\s+"
                r"\d{4}\s*[-–]\s*\d{1,2}:\d{2}\b",
                text.lower(),
            )
            if match:
                dt = parse_italian_date(match.group(0))
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
    DB_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def make_rss(items):
    now = datetime.now(timezone.utc)

    xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:media="http://search.yahoo.com/mrss/">',
        "<channel>",
        "<title>Il Giornale - Ultime notizie</title>",
        f"<link>{BASE}/</link>",
        "<description>Feed RSS personale delle ultime notizie pubblicate da il Giornale</description>",
        "<language>it-IT</language>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
        (
            f'<atom:link href="{FEED_URL}" '
            'rel="self" type="application/rss+xml" />'
        ),
    ]

    for item in items[:MAX_ITEMS]:
        dt = parse_iso(item.get("pubdate"))
        if not dt:
            continue

        xml.extend(
            [
                "<item>",
                f"<title>{esc(item.get('title'))}</title>",
                f"<link>{esc(item.get('url'))}</link>",
                f'<guid isPermaLink="true">{esc(item.get("url"))}</guid>',
                f"<pubDate>{format_datetime(dt)}</pubDate>",
                f"<description>{esc(item.get('summary'))}</description>",
            ]
        )

        if item.get("image"):
            xml.append(
                f'<media:content url="{esc(item["image"])}" medium="image" />'
            )

        xml.append("</item>")

    xml.extend(["</channel>", "</rss>"])
    FEED_FILE.write_text("\n".join(xml), encoding="utf-8")


def sort_items(items):
    def key(item):
        dt = parse_iso(item.get("pubdate"))
        return dt or datetime.min.replace(tzinfo=timezone.utc)

    return sorted(items, key=key, reverse=True)


def main():
    now = datetime.now(timezone.utc)
    db = load_db()

    by_url = {
        item["url"]: item
        for item in db
        if isinstance(item, dict) and item.get("url")
    }

    # ------------------------------------------------------------
    # 1. Find new article URLs on the homepage.
    # ------------------------------------------------------------
    candidates = homepage_candidates()

    new_candidates = [
        item for item in candidates
        if item["url"] not in by_url
    ][:MAX_NEW_PER_RUN]

    print(f"Link candidati in homepage: {len(candidates)}")
    print(f"Nuovi URL rispetto al database: {len(new_candidates)}")

    # ------------------------------------------------------------
    # 2. For NEW articles, get the date from the article page.
    #    If no reliable date is found, DO NOT add the article.
    #    This is safer than assigning the current time.
    # ------------------------------------------------------------
    added = 0

    for candidate in new_candidates:
        dt = article_publication_date(candidate["url"])

        if dt is None:
            print(f"SKIP senza data affidabile: {candidate['url']}")
            continue

        candidate["pubdate"] = dt.isoformat()
        by_url[candidate["url"]] = candidate
        added += 1

    # ------------------------------------------------------------
    # 3. Repair old bad dates.
    #
    # The previous scraper could have stored a future date such as
    # 19 Oct 2026 because that date appeared inside the article text.
    # Re-check every item whose stored date is in the future.
    # ------------------------------------------------------------
    future_limit = now + timedelta(hours=2)
    repaired = 0

    for item in list(by_url.values()):
        old_dt = parse_iso(item.get("pubdate"))

        if old_dt and old_dt > future_limit:
            real_dt = article_publication_date(item["url"])

            if real_dt:
                item["pubdate"] = real_dt.isoformat()
                repaired += 1
                print(
                    f"RIPARATA DATA: {item['title']} -> "
                    f"{item['pubdate']}"
                )

    # ------------------------------------------------------------
    # 4. Sort newest first and keep 100.
    # ------------------------------------------------------------
    items = sort_items(list(by_url.values()))[:MAX_ITEMS]

    save_db(items)
    make_rss(items)

    newest = items[0] if items else {}

    print(f"Nuovi articoli aggiunti: {added}")
    print(f"Date future riparate: {repaired}")
    print(f"Articoli nel feed: {len(items)}")
    print(f"Più recente: {newest.get('title', 'nessuno')}")
    print(f"Data più recente: {newest.get('pubdate', '-')}")


if __name__ == "__main__":
    main()
