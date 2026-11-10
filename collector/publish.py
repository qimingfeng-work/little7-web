#!/usr/bin/env python3
"""Publish rewritten articles (status=1) as Hugo markdown posts.

Usage: venv/bin/python collector/publish.py [--limit N]
"""
import argparse
import os
import re
import sqlite3
from datetime import datetime
from email.utils import parsedate_to_datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "hotnews.db")
CONTENT = os.path.join(BASE, "site", "content", "posts")


def slugify(title):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return s[:60] or "post"


def yaml_str(s):
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, title, title_lang, body, summary, source_url, source_name, lang, "
        "       published_at, mode, image_url FROM articles "
        "WHERE status=1 ORDER BY published_at DESC LIMIT ?", (args.limit,)).fetchall()
    n = 0
    for aid, title, title_lang, body, summary, src_url, src_name, lang, published, mode, image_url in rows:
        try:
            d = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            d = parsedate_to_datetime(published).astimezone()
        disp_title = title_lang or title
        outdir = os.path.join(CONTENT, str(d.year), f"{d.month:02d}")
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(outdir, f"{slugify(disp_title)}-{aid}.md")
        # No exists-check: the file may hold a stale digest version from an
        # earlier publish; the DB is the source of truth and must overwrite it.
        text = body or summary or disp_title
        # Hugo .Summary = first 70 whitespace words (no paragraph awareness):
        # EN spans paragraphs, CJK swallows the whole body. Pin the excerpt to
        # the first paragraph with a <!--more--> marker.
        if text and "\n\n" in text and "<!--more-->" not in text:
            p1, rest = text.split("\n\n", 1)
            text = p1 + "\n\n<!--more-->\n\n" + rest
        fm = ("---\n"
              f"title: {yaml_str(disp_title)}\n"
              f"date: {published}\n"
              f"lang: {yaml_str(lang)}\n"
              f"mode: {yaml_str(mode)}\n"
              f"source_url: {yaml_str(src_url)}\n"
              f"source_name: {yaml_str(src_name)}\n"
              + (f"image_url: {yaml_str(image_url)}\n" if image_url else "")
              + "draft: false\n---\n\n")
        post = (fm + text + "\n\n---\n\n"
                "*Aggregated summary. Original: "
                f"[{src_name or 'source'}]({src_url})*\n")
        with open(path, "w") as f:
            f.write(post)
        conn.execute("UPDATE articles SET status=2, updated_at=? WHERE id=?",
                     (datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"), aid))
        n += 1
    conn.commit()
    conn.close()
    print(f"published={n}")


if __name__ == "__main__":
    main()
