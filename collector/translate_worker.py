#!/usr/bin/env python3
"""Translation worker: continuously translates published articles into
zh/ja/fr/ar and writes Hugo .<lang>.md files next to the English version.
Independent of the pipeline cron; paces itself to share the LLM gateway.

Config (env):
  TRANSLATE_BATCH (articles per batch, default 1),
  TRANSLATE_SLEEP (seconds between batches, default 0),
  TRANSLATE_IDLE (seconds to idle when everything is translated, default 300).
Log: logs/translate_worker.log
"""
import random
import sys, os, sqlite3, httpx, re, glob, time, subprocess, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rewrite import load_env, get_conn
from publish import yaml_str

# OpenCC: normalise LLM output to Traditional Chinese (Taiwan) before writing.
# The LLM sometimes returns Simplified characters despite the prompt; converting
# here guarantees every zh-TW file is Traditional.
try:
    import opencc
    _tw = opencc.OpenCC('s2twp')
except Exception:
    _tw = None

def to_tw(text):
    if _tw is None or not text:
        return text
    return _tw.convert(text)

load_env()

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT = os.path.join(BASE, "site", "content", "posts")
LOG = os.path.join(BASE, "logs", "translate_worker.log")

BATCH = int(os.environ.get("TRANSLATE_BATCH", "1"))
SLEEP = int(os.environ.get("TRANSLATE_SLEEP", "0"))
IDLE = int(os.environ.get("TRANSLATE_IDLE", "300"))
LANG_SLEEP = int(os.environ.get("TRANSLATE_LANG_SLEEP", "10"))

# Shared model list; each task picks its own API keys
# Model list. TRANSLATE_MODELS pins this worker to specific models (useful
# when one model's free-tier quota is spent but another still has room);
# falls back to the shared LLM_MODELS list.
_LLM_MODELS_ENV = os.environ.get("TRANSLATE_MODELS") or os.environ.get("LLM_MODELS")
if _LLM_MODELS_ENV:
    _MODELS = [m.strip() for m in _LLM_MODELS_ENV.split(",") if m.strip()]
else:
    _MODELS = [os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")]

_KEYS = [os.environ.get("TRANSLATE_API_KEYS") or os.environ.get("TRANSLATE_API_KEY") or os.environ.get("LLM_API_KEY", "")]

BASE_URL = (os.environ.get("TRANSLATE_BASE_URL") or
            os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/")


class QuotaExceeded(Exception):
    """HTTP 429 RESOURCE_EXHAUSTED: free-tier daily quota is spent.
    Carries the server-provided retry delay so the caller sleeps it off
    instead of burning requests on doomed retries."""

    def __init__(self, delay, msg="quota exceeded"):
        self.delay = max(float(delay), 60.0)
        super().__init__(f"{msg} (retry in {self.delay:.0f}s)")


class BatchParseError(Exception):
    """LLM output was not the expected JSON — usually a truncated batch."""


def quota_delay(resp):
    """Best-effort retry delay (seconds) from a 429 response body."""
    try:
        err = resp.json().get("error", {})
    except Exception:
        return 3600.0
    for det in err.get("details", []) or []:
        if str(det.get("@type", "")).endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", str(det.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    m = re.search(r"retry in (\d+)h(\d+)m([\d.]+)s", str(err.get("message", "")))
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return 3600.0


def _pick_model():
    """Randomly select a model/key pair for each request."""
    idx = random.randint(0, len(_MODELS) - 1)
    return _MODELS[idx], _KEYS[idx % len(_KEYS)]

LANGS = {
    'zh-TW': '你是新聞翻譯編輯。把下面的英文文章翻譯改寫為地道繁體中文（台灣用語，500-1000字）：保持全部事實準確，語氣中立可讀，不逐句照抄，結尾加一行「來源: {src}」。輸出格式：第一行 TITLE: <繁體中文標題>；之後是正文。不要其他內容。',
}

def segment(text):
    """Split a translated body into paragraphs (2-3 sentences each) so Hugo's
    automatic summary (first paragraph) shows a short excerpt, not the whole
    article. Sentence ends: CJK 。！？…, Latin .!?, Arabic ؟.
    """
    parts = re.split(r"(?<=[。！？.!?…？])", text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return text
    src_line = None
    for i, p in enumerate(parts):
        if re.match(r"^(来源|來源|出典|Sources|Source|المصدر)[:：]", p):
            src_line = parts.pop(i)
            break
    paras, cur, sent = [], "", 0
    for p in parts:
        if cur and (sent >= 2 or len(cur) + len(p) > 130):
            paras.append(cur)
            cur, sent = p, 1
        else:
            cur += p
            sent += 1
    if cur:
        paras.append(cur)
    if src_line:
        paras.append(src_line)
    return "\n\n".join(paras)

def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def translate_single(client, text, src, lang, tries=3):
    """Translate a single article in one language (fallback)."""
    for attempt in range(tries):
        try:
            model, key = _pick_model()
            content = LANGS[lang].format(src=src) + "\n\nARTICLE:\n" + text
            if model.startswith("gemini"):
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                r = client.post(url,
                                json={
                                    "contents": [{"parts": [{"text": content}]}],
                                    "generationConfig": {
                                        "maxOutputTokens": 32768,
                                        "temperature": 0.3
                                    }
                                },
                                timeout=120)
                if r.status_code == 429:
                    raise QuotaExceeded(quota_delay(r))
                r.raise_for_status()
                data = r.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    raise ValueError("empty LLM output (no candidates)")
                parts_list = candidates[0].get("content", {}).get("parts", [])
                if not parts_list:
                    raise ValueError("empty LLM output (no parts)")
                out = parts_list[0].get("text", "").strip()
                if not out:
                    raise ValueError("empty LLM output (blank text)")
            else:
                r = client.post(f"{BASE_URL}/chat/completions",
                                headers={"Authorization": f"Bearer {key}"},
                                json={"model": model,
                                      "messages": [{"role": "user", "content": content}]},
                                timeout=120)
                r.raise_for_status()
                out = r.json()["choices"][0]["message"]["content"].strip()
            m = re.match(r"^TITLE:\s*(.+?)\s*\n+(.*)$", out, re.S)
            if m:
                title, body = m.group(1).strip(), m.group(2).strip()
            else:
                lines = out.split("\n")
                title, body = lines[0][:80], "\n".join(lines[1:]).strip() or out
            if not title or not body:
                raise ValueError("empty LLM output (reasoning-only response?)")
            return title, body
        except QuotaExceeded:
            raise
        except Exception as e:
            if attempt == tries - 1:
                raise
            time.sleep(30 * (2 ** attempt))

def translate_multi(client, articles, languages, tries=3):
    """
    Translate multiple articles into multiple languages in one API call.
    articles: list of tuples (id, body, source_url, source_name, published, title)
    languages: list of lang codes, e.g. ['zh','ja','fr','ar']
    returns: dict {article_id: {lang: (title, body)}}
    """
    if not articles or not languages:
        return {}

    # Build prompt: ask for JSON mapping of article_id -> lang -> (title, body)
    parts = []
    if languages[0] == 'fr':
        parts.append("Vous êtes un rédacteur de traduction de presse. Traduisez les articles anglais suivants dans la langue demandée, chaque traduction de 500 à 1000 mots. Si le texte original est court, ajoutez des éléments de contexte et des détails dans la traduction, ne réduisez pas en résumé.")
    elif languages[0] == 'zh-TW':
        parts.append("你是新聞翻譯編輯。請將下面的英文文章翻譯成對應語言，每篇譯文保持 500–1000 字。如果原文較短，請在譯文中補充相關背景說明和細節，不要壓縮成摘要。若目標語言為中文，請使用繁體中文（台灣用語）。")
    else:
        parts.append("你是新闻翻译编辑。请将下面的英文文章翻译成对应语言，每篇译文保持 500–1000 字。如果原文较短，请在译文中补充相关背景说明和细节，不要压缩成摘要。")
    for lg in languages:
        lang_name = {
            'zh-TW': '繁體中文（zh-TW）',
            'zh': '中文（zh）',
            'ja': '日文（ja）',
            'fr': '法文（fr）',
            'ar': '阿拉伯文（ar）',
        }.get(lg, lg)
        parts.append(f"- {lang_name}")
    parts.append("请对每篇文章和每种语言分别给出标题和正文，输出为一个 JSON 数组，数组元素格式为：")
    # Build example dynamically based on requested languages
    example_langs = ", ".join(languages)
    example_obj = ", ".join(
        f'{lg}: {{ title: "<{lg}标题>", body: "<{lg}正文>" }}' for lg in languages
    )
    parts.append(f'{{ article_id: <數字>, {example_obj} }}')
    if languages[0] == 'fr':
        parts.append("Gardez tous les faits exacts, ton neutre et lisible, et terminez par une ligne \"Source: {src}\".")
    elif languages[0] == 'zh-TW':
        parts.append("保持事實準確、語氣中立可讀，結尾加「來源: {src}」。")
    else:
        parts.append("保持事实准确、语气中立、可读，结尾加“来源: {src}”。")
    parts.append("不要輸出任何解釋性文字，只輸出 JSON 數組。")

    for aid, body, src_url, src_name, published, title in articles:
        parts.append(f"\n### 文章 {aid}")
        parts.append(f"来源: {src_name or 'the original publication'}")
        parts.append("原文:")
        parts.append(body)
        parts.append("")

    prompt = "\n".join(parts)

    for attempt in range(tries):
        try:
            model, key = _pick_model()
            if model.startswith("gemini"):
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                r = client.post(url,
                                json={
                                    "contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {
                                        "maxOutputTokens": 32768,
                                        "temperature": 0.3
                                    }
                                },
                                timeout=300)
                if r.status_code == 429:
                    raise QuotaExceeded(quota_delay(r))
                r.raise_for_status()
                data = r.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    raise ValueError("empty LLM output (no candidates)")
                parts_list = candidates[0].get("content", {}).get("parts", [])
                if not parts_list:
                    raise ValueError("empty LLM output (no parts)")
                out = parts_list[0].get("text", "").strip()
                if not out:
                    raise ValueError("empty LLM output (blank text)")
            else:
                r = client.post(f"{BASE_URL}/chat/completions",
                                headers={"Authorization": f"Bearer {key}"},
                                json={"model": model,
                                      "messages": [{"role": "user", "content": prompt}]},
                                timeout=300)
                r.raise_for_status()
                out = r.json()["choices"][0]["message"]["content"].strip()

            # Parse JSON
            try:
                parsed = json.loads(out)
            except Exception:
                parsed = None
            if parsed is None:
                raise BatchParseError("unparseable LLM output (likely truncated)")
            if not isinstance(parsed, list):
                raise BatchParseError("expected JSON array")
            result = {}
            for item in parsed:
                if not isinstance(item, dict):
                    raise ValueError("Item is not a dict")
                aid = item.get("article_id")
                if aid is None:
                    continue
                rec = {}
                for lang in languages:
                    rec[lang] = (item.get(lang, {}).get("title", ""), item.get(lang, {}).get("body", ""))
                result[aid] = rec
            return result
        except QuotaExceeded:
            raise
        except Exception as e:
            if attempt == tries - 1:
                raise
            time.sleep(30 * (2 ** attempt))
    raise RuntimeError("unreachable")

def translate_batch(client, batch, languages):
    """Translate one batch. A batch too large for maxOutputTokens comes back as
    unparseable JSON; halve it and retry so one long article cannot stall the
    whole queue."""
    try:
        return translate_multi(client, batch, languages=languages)
    except QuotaExceeded:
        raise
    except BatchParseError as e:
        if len(batch) <= 1:
            raise
        log(f"batch of {len(batch)} unparseable ({e}); retrying as two halves")
        mid = len(batch) // 2
        merged = {}
        for half in (batch[:mid], batch[mid:]):
            if not half:
                continue
            try:
                merged.update(translate_batch(client, half, languages))
            except QuotaExceeded:
                raise
            except Exception as e2:
                log(f"half-batch failed: {e2}")
        return merged


def main():
    c = get_conn()
    client = httpx.Client()
    log(f"worker start (batch={BATCH} sleep={SLEEP} idle={IDLE} lang_sleep={LANG_SLEEP})")
    # 指数退避：batch 失败时逐步拉长重试间隔
    backoff = SLEEP
    MAX_BACKOFF = 300
    while True:
        # collect existing translate ids
        existing = set()
        for f in glob.glob(os.path.join(CONTENT, "**", "*.md"), recursive=True):
            m = re.search(r"-(\d+)\.(zh-tw|zh|ja|fr|ar)\.md$", f)
            if m:
                existing.add((int(m.group(1)), m.group(2)))

        # Fetch pending rows, limited to BATCH per pass
        rows = c.execute(
            "SELECT id, body, source_url, source_name, published_at, title, image_url FROM articles "
            "WHERE status=2 AND body IS NOT NULL AND length(body) > 200 "
            "ORDER BY id DESC"
        ).fetchall()

        todo = []
        todo_meta = {}  # aid -> (src_url, src_name, published, image_url)
        for aid, body, src_url, src_name, published, title, image_url in rows:
            missing = False
            for lg in LANGS:
                if (aid, lg) not in existing:
                    missing = True
                    break
            if missing:
                todo.append((aid, body, src_url, src_name, published, title))
                todo_meta[aid] = (src_url, src_name, published, image_url)
                if len(todo) >= BATCH:
                    break

        if not todo:
            time.sleep(IDLE)
            continue

        # Try to translate a batch of up to BATCH articles into all languages at once
        try:
            result = translate_batch(client, todo, list(LANGS.keys()))
            backoff = SLEEP  # 成功，重置退避
        except QuotaExceeded as e:
            log(f"{e}; sleeping until quota reset")
            time.sleep(min(e.delay + 60, 86400))
            continue
        except Exception as e:
            log(f"batch translate FAIL: {e}")
            backoff = min(backoff * 2, MAX_BACKOFF)
            log(f"backoff to {backoff}s")
            time.sleep(backoff)
            continue

        # Process successful batch results
        for aid, rec in result.items():
            for lg, (tt, bb) in rec.items():
                if (aid, lg) in existing:
                    continue
                seg_body = segment(bb)
                if "\n\n" in seg_body and "<!--more-->" not in seg_body:
                    p1, rest = seg_body.split("\n\n", 1)
                    seg_body = p1 + "\n\n<!--more-->\n\n" + rest
                en_files = glob.glob(os.path.join(CONTENT, "**", f"*-{aid}.md"), recursive=True)
                if not en_files:
                    log(f"skip id={aid}: no en file")
                    continue
                en = en_files[0]
                base = os.path.basename(en)[:-3]
                lp = os.path.join(os.path.dirname(en), f"{base}.{lg.lower()}.md")
                if os.path.exists(lp):
                    continue
                # zh-TW: force Traditional Chinese (LLM may return Simplified)
                if lg.lower() == "zh-tw":
                    tt = to_tw(tt)
                    seg_body = to_tw(seg_body)
                fm = (
                    "---\n"
                    f"title: {yaml_str(tt)}\n"
                    f"date: {todo_meta[aid][2]}\n"
                    f'lang: "{lg}"\n'
                    f'mode: "llm_translate"\n'
                    f"source_url: {yaml_str(todo_meta[aid][0])}\n"
                    f"source_name: {yaml_str(todo_meta[aid][1])}\n"
                    + (f"image_url: {yaml_str(todo_meta[aid][3])}\n" if todo_meta[aid][3] else "")
                    + "draft: false\n---\n\n"
                )
                with open(lp, "w") as f:
                    f.write(fm + seg_body + "\n")
                log(f"id={aid} lang={lg} -> {os.path.basename(lp)} ({len(bb)} chars)")

        time.sleep(backoff)

if __name__ == "__main__":
    main()
