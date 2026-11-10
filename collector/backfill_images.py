#!/usr/bin/env python3
"""Backfill image_url for existing articles by re-fetching RSS feeds."""
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect import extract_image, fetch_rss, get_conn
import httpx
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(BASE, "collector", "sources.json")


def main():
    conn = get_conn()
    # Get articles without image_url
    rows = conn.execute(
        "SELECT id, title, source_url, source_name FROM articles WHERE image_url IS NULL"
    ).fetchall()
    print(f"Articles without image: {len(rows)}")

    # Build a map of URL -> image_url from RSS feeds
    url_to_image = {}
    with open(SOURCES) as f:
        sources = json.load(f)["sources"]

    client = httpx.Client()
    for cfg in sources:
        try:
            items = fetch_rss(client, cfg)
            for it in items:
                if it["image_url"]:
                    url_to_image[it["url"]] = it["image_url"]
        except Exception as e:
            print(f"FAIL {cfg['name']}: {e}")

    print(f"RSS items with images: {len(url_to_image)}")

    # Update articles by matching source_url
    updated = 0
    for aid, title, source_url, source_name in rows:
        img = url_to_image.get(source_url)
        if img:
            conn.execute("UPDATE articles SET image_url=? WHERE id=?", (img, aid))
            updated += 1

    conn.commit()
    print(f"Updated: {updated}")
    conn.close()


if __name__ == "__main__":
    main()
