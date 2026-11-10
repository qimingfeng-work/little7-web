#!/usr/bin/env python3
"""HotNews collector: fetch RSS sources -> SQLite with URL dedup.

Usage: /home/ubuntu/hotnews/venv/bin/python collector/collect.py [--per-source N] [--source NAME]
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import feedparser
import httpx

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(BASE, "collector", "sources.json")
DB = os.path.join(BASE, "hotnews.db")
UA = "Mozilla/5.0 (compatible; HotNewsBot/1.0; +http://localhost/about)"
TIMEOUT = httpx.Timeout(15)

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    source_url TEXT NOT NULL,
    source_name TEXT,
    lang TEXT DEFAULT 'en',
    published_at TEXT,
    status INTEGER DEFAULT 0,
    mode TEXT DEFAULT 'rss',
    body TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON articles(status);
"""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def load_sources():
    with open(SOURCES) as f:
        return json.load(f)["sources"]


# Junk-entry filter: Google News occasionally emits nav/promo entries
# (e.g. title "Download now", summary "Top stories, breaking news..."),
# which are not news and would be published as articles.
JUNK_TITLES = [
    "download now", "sign up now", "top stories", "sign in", "subscribe",
    "watch live", "trending now", "sponsored", "advertisement",
    "follow news topics", "send us feedback", "learn more",
]
MIN_SUMMARY_LEN = 40


def is_junk(title, summary):
    t = title.lower()
    if any(j in t for j in JUNK_TITLES):
        return True
    if len(summary) < MIN_SUMMARY_LEN:
        return True
    return False


def clean_title(title, split):
    """Google News titles look like 'Headline - Publication'; keep the last
    ' - ' split so headlines containing a dash are not broken."""
    if split:
        m = re.match(r"^(.*?)\s+-\s+([^\-]+)$", title)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return title.strip(), None


def extract_image(entry):
    """Extract image URL from RSS entry (media_content > media_thumbnail > enclosure > img tag)."""
    # media_content (large images, e.g. The Guardian, Ars Technica)
    mc = entry.get("media_content")
    if mc and isinstance(mc, list) and len(mc) > 0:
        url = mc[0].get("url")
        if url:
            return url
    # media_thumbnail (small thumbnails, e.g. BBC)
    mt = entry.get("media_thumbnail")
    if mt and isinstance(mt, list) and len(mt) > 0:
        url = mt[0].get("url")
        if url:
            return url
    # enclosure (podcasts/media)
    enc = entry.get("enclosure")
    if enc and isinstance(enc, dict):
        url = enc.get("url")
        if url:
            return url
    # img tag in summary
    summary = entry.get("summary", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', summary)
    if m:
        return m.group(1)
    return None


def fetch_rss(client, cfg):
    items = []
    resp = client.get(cfg["url"], headers={"User-Agent": UA},
                      timeout=TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    for entry in feed.entries:
        title, sep_source = clean_title(entry.get("title", ""), cfg.get("split_title", False))
        if not title:
            continue
        url = entry.get("link", "")
        if not url:
            continue
        summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", entry.get("summary", ""))).strip()
        if is_junk(title, summary):
            continue
        src = None
        src_dict = entry.get("source")
        if src_dict:
            src = src_dict.get("title")
        if not src and sep_source:
            src = sep_source
        items.append({
            "title": title,
            "summary": summary,
            "url": url,
            "source_name": src or cfg.get("name", ""),
            "published": entry.get("published") or entry.get("updated") or utcnow(),
            "image_url": extract_image(entry),
        })
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-source", type=int, default=30)
    ap.add_argument("--source", default=None)
    args = ap.parse_args()

    conn = get_conn()
    sources = load_sources()
    client = httpx.Client()
    added = skipped = 0
    failed = []

    for cfg in sources:
        if args.source and cfg["name"] != args.source:
            continue
        try:
            items = fetch_rss(client, cfg)
        except Exception as e:
            failed.append(f"{cfg['name']}: {e}")
            continue
        time.sleep(1)
        for it in items[: args.per_source]:
            h = hashlib.sha256(it["url"].encode()).hexdigest()[:40]
            now = utcnow()
            # Title-level dedup: same story often appears in several feeds
            # (BBC main + BBC World, Google News re-listing direct feeds) with
            # different URLs; keep only the first copy.
            if conn.execute(
                    "SELECT 1 FROM articles WHERE lower(title)=lower(?)",
                    (it["title"],)).fetchone():
                skipped += 1
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO articles "
                "(url_hash, title, summary, source_url, source_name, lang, "
                " published_at, status, created_at, updated_at, image_url) "
                "VALUES (?,?,?,?,?,?,?,0,?,?,?)",
                (h, it["title"], it["summary"], it["url"], it["source_name"],
                 cfg.get("lang", "en"), it["published"], now, now, it["image_url"]))
            added += cur.rowcount
            skipped += 1 - cur.rowcount
    conn.commit()
    print(f"added={added} skipped_dup={skipped} failed={len(failed)}")
    for f in failed:
        print("FAIL:", f)
    conn.close()


if __name__ == "__main__":
    main()
