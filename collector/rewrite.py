#!/usr/bin/env python3
"""Rewrite collected articles into original articles (factual rewrite, not
translation). Uses an LLM when a key is available; otherwise falls back to
digest mode (publishes the real RSS summary + source link).

Key config (any OpenAI-compatible endpoint):
  LLM_API_KEY  -> required for LLM mode
  LLM_BASE_URL -> default https://api.openai.com/v1
  LLM_MODEL    -> default gpt-4o-mini
  REWRITE_API_KEY -> rewrite LLM config (any OpenAI-compatible or Gemini endpoint)
  TARGET_LANG  -> default en (language of the rewritten title + body)
  REWRITE_FETCH -> 1/true: also fetch the source article text for the LLM (default off)

Usage: venv/bin/python collector/rewrite.py [--limit N]
"""
import argparse
import json
import os
import random
import re
import sqlite3
from datetime import datetime, timezone

import httpx

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "hotnews.db")
TARGET_LANG = os.environ.get("TARGET_LANG", "en")


def load_env():
    """Load collector/.env into os.environ if present (does not override
    values already set in the environment)."""
    env_path = os.path.join(BASE, "collector", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


load_env()

# LLM shared config
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")

# Rewrite task picks its own API keys from the shared model list
_LLM_MODELS_ENV = os.environ.get("LLM_MODELS")
if _LLM_MODELS_ENV:
    _MODELS = [m.strip() for m in _LLM_MODELS_ENV.split(",") if m.strip()]
else:
    _MODELS = [os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")]

_KEYS = [os.environ.get("REWRITE_API_KEYS") or os.environ.get("REWRITE_API_KEY") or os.environ.get("LLM_API_KEY", "")]

# Choose a random model/key pair for this run
_model_idx = random.randint(0, len(_MODELS) - 1)
REWRITE_MODEL = _MODELS[_model_idx]
REWRITE_API_KEY = _KEYS[_model_idx % len(_KEYS)]
# Export the selected config as environment variables for downstream code
os.environ["REWRITE_API_KEY"] = REWRITE_API_KEY
os.environ["REWRITE_MODEL"] = REWRITE_MODEL

PROMPT = """You are a news rewrite editor. Given the source news item (title + summary), write a NEW article in {lang} of 600-1000 words that:
IMPORTANT: The output MUST be in {lang} only. Even if the source title or summary is in another language (e.g., Chinese), you must translate and rewrite everything into {lang}. Do NOT output any text in the source language.
- reports the same facts (who/what/when/where/why) fully in your own words
- does NOT copy any sentence from the source material
- is standalone, neutral and readable for a general audience
- ends with a final line: "Sources: <source name in English>"
- Use multiple paragraphs with blank lines between them for readability
Return ONLY: first line "TITLE: <title in {lang}>", then the article body with proper paragraph breaks. No markdown headers, no other text.
If the original title is not in {lang}, translate it to {lang}."""

# Used when REWRITE_FETCH=1: the LLM also sees the fetched article text.
PROMPT_ARTICLE = """You are a news rewrite editor. Given the source news item (title, summary and full article text), write a NEW article in {lang} of 600-1000 words that:
IMPORTANT: The output MUST be in {lang} only. Even if the source title or summary is in another language (e.g., Chinese), you must translate and rewrite everything into {lang}. Do NOT output any text in the source language.
- reports the same facts (who/what/when/where/why) fully in your own words
- bases every claim ONLY on the article text; do not invent details
- does NOT copy any sentence from the source material
- is standalone, neutral and readable for a general audience
- ends with a final line: "Sources: <source name in English>"
- Use multiple paragraphs with blank lines between them for readability
Return ONLY: first line "TITLE: <title in {lang}>", then the article body with proper paragraph breaks. No markdown headers, no other text.
If the original title is not in {lang}, translate it to {lang}."""

ARTICLE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ARTICLE_TEXT_CAP = 4000


def extract_article_text(html):
    """Extract main text: prefer <article>, then <main>, then whole page;
    drop script/style and short nav junk; decode entities; cap length."""
    import html as _html
    html = re.sub(r"(?is)<script.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?</style>", " ", html)
    candidates = []
    for tag in ("article", "main"):
        m = re.search(rf"(?is)<{tag}[^>]*>(.*?)</{tag}>", html)
        if m:
            candidates.append(m.group(1))
    candidates.append(html)

    def blocks(chunk):
        parts = re.findall(r"(?is)<(?:p|h[1-3]|li)[^>]*>(.*?)</(?:p|h[1-3]|li)>", chunk)
        out = []
        for p in parts:
            t = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", p)).strip()
            t = _html.unescape(t)
            if len(t) >= 60:  # drop short nav/promo junk
                out.append(t)
        return out

    for chunk in candidates:
        text = " ".join(blocks(chunk))
        if len(text) >= 200:
            return text[:ARTICLE_TEXT_CAP]
    text = _html.unescape(re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip())
    return text[:ARTICLE_TEXT_CAP]


def fetch_article(client, url):
    """Fetch the source article page; returns extracted text or '' on failure."""
    if not url:
        return ""
    try:
        r = client.get(url, headers={"User-Agent": ARTICLE_UA},
                       timeout=25, follow_redirects=True)
        r.raise_for_status()
        text = extract_article_text(r.text)
        return text if len(text) >= 200 else ""
    except Exception:
        return ""


def parse_title_body(out):
    """Split LLM output into (title, body); expects first line 'TITLE: ...'."""
    out = out.lstrip("\n")  # AGNES 响应有时带前导换行
    m = re.match(r"^TITLE:\s*(.+?)\s*\n+(.*)$", out, re.S)
    if m:
        title, body = m.group(1).strip(), m.group(2).strip()
    else:
        lines = out.split("\n")
        title, body = lines[0][:80], "\n".join(lines[1:]).strip() or out
    if not title or not body:
        raise ValueError("empty LLM output")
    return title, body


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def llm_rewrite_retry(client, title, summary, src, url=None, tries=3):
    """Call the LLM with retries; the Volcengine coding gateway occasionally
    hangs a request (ESTAB but no response). A 120s read timeout + retry on a
    fresh request recovers it."""
    import time
    for attempt in range(tries):
        try:
            return llm_rewrite(client, title, summary, src, url)
        except Exception as e:
            if attempt == tries - 1:
                raise
            # Mask the API key in any log output
            err_msg = str(e)
            if "key=" in err_msg:
                err_msg = err_msg.replace("key=", "key=[REDACTED]")
            print(f"  retry {attempt + 1} for '{title[:50]}': {err_msg}")
            time.sleep(30 * (2 ** attempt))
    raise RuntimeError("unreachable")


def get_conn():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def llm_rewrite(client, title, summary, src, url=None):
    """Returns (title_lang, body) — the title translated/rewritten into
    TARGET_LANG, and the rewritten article body."""
    fetch_mode = os.environ.get("REWRITE_FETCH", "").strip().lower() in ("1", "true", "yes", "on")
    article = fetch_article(client, url) if fetch_mode else ""
    if article:
        prompt = PROMPT_ARTICLE.format(lang=TARGET_LANG, src=src)
        content = f"{prompt}\n\nTITLE: {title}\nSUMMARY: {summary}\n\nARTICLE TEXT:\n{article}"
    else:
        prompt = PROMPT.format(lang=TARGET_LANG, src=src)
        content = f"{prompt}\n\nTITLE: {title}\nSUMMARY: {summary}"
    # 统一走 REWRITE_* 配置:基线地址里带 generativelanguage 即 Gemini 格式,
    # 其他则按 OpenAI 兼容格式。无单独 Gemini 密钥配置.
    idx = random.randint(0, len(_MODELS) - 1)
    model = _MODELS[idx]
    key = _KEYS[idx % len(_KEYS)]
    base = LLM_BASE_URL
    if "generativelanguage.googleapis.com" in base:
        r = client.post(
            f"{base}/models/{model}:generateContent",
            params={"key": key},
            json={
                "contents": [{"parts": [{"text": content}]}],
                "generationConfig": {
                    "maxOutputTokens": 16384,
                    "temperature": 0.4
                }
            },
            timeout=120)
    else:
        r = client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model,
                  "messages": [{"role": "user", "content": content}]},
            timeout=120)
    r.raise_for_status()
    body = r.json()
    if body.get("candidates"):
        # Gemini 格式
        candidates = body.get("candidates") or []
        if not candidates:
            raise ValueError("empty LLM output (no candidates)")
        parts_list = candidates[0].get("content", {}).get("parts", [])
        if not parts_list:
            raise ValueError("empty LLM output (no parts)")
        text = parts_list[0].get("text", "").strip()
        if not text:
            raise ValueError("empty LLM output (blank text)")
    else:
        # OpenAI 兼容格式
        choices = body.get("choices") or []
        if not choices:
            raise ValueError("empty LLM output (no choices)")
        text = choices[0].get("message", {}).get("content", "").strip()
        if not text:
            raise ValueError("empty LLM output (blank content)")
    return parse_title_body(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    has_llm = bool(os.environ.get("LLM_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, summary, source_name, source_url FROM articles "
        "WHERE status=0 AND summary IS NOT NULL AND length(summary) > 40 "
        "ORDER BY published_at DESC LIMIT ?", (args.limit,)).fetchall()
    client = httpx.Client()
    n = 0
    for aid, title, summary, src, url in rows:
        try:
            if has_llm:
                title_lang, body = llm_rewrite_retry(client, title, summary, src or "the original publication", url)
                mode = "llm_rewrite"
            else:
                body = summary
                title_lang = None
                mode = "digest"
            conn.execute(
                "UPDATE articles SET body=?, title_lang=?, status=1, mode=?, updated_at=? WHERE id=?",
                (body, title_lang, mode, utcnow(), aid))
            conn.commit()  # per-article commit: progress survives crashes
            n += 1
            print(f"  [{n}] {title[:70]}", flush=True)
        except Exception as e:
            print(f"FAIL id={aid}: {e}", flush=True)
    conn.close()
    fetch = os.environ.get("REWRITE_FETCH", "").strip().lower() in ("1", "true", "yes", "on")
    print(f"rewritten={n} mode={'llm' if has_llm else 'digest'} "
          f"lang={TARGET_LANG} fetch={'on' if fetch else 'off'} "
          f"(set TARGET_LANG / LLM_API_KEY / REWRITE_FETCH env vars to change)")


if __name__ == "__main__":
    main()

