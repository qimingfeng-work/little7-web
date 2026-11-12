#!/usr/bin/env python3
"""Rewrite worker: continuously rewrites pending articles (status=0) into
status=1 (LLM rewrite with fetched article text + translated title), then
publishes and builds. Runs independently of the cron pipeline as a systemd
service; the cron keeps collect/publish/build only.

Config (env): REWRITE_BATCH (articles per cycle, default 1),
REWRITE_SLEEP (seconds between batches, default 5),
REWRITE_IDLE (seconds when the queue is empty, default 120).
Log: logs/rewrite_worker.log
"""
import sys
import os
import sqlite3
import time
import subprocess
import json
import random
import signal
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rewrite import (
    llm_rewrite_retry,
    get_conn,
    utcnow,
    load_env,
    _MODELS,
    _KEYS,
)
load_env()

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(BASE, "logs", "rewrite_worker.log")
BATCH = int(os.environ.get("REWRITE_BATCH", "10"))
SLEEP = int(os.environ.get("REWRITE_SLEEP", "5"))
IDLE = int(os.environ.get("REWRITE_IDLE", "120"))


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def batch_llm_rewrite(client, articles):
    """Rewrite multiple articles in one LLM request.

    articles: list of dict with keys id, title, summary, src, url
    returns: dict {id: (title_lang, body)}
    """
    last_err = None
    # Try each model in turn: Gemini free tiers return 503 "high demand" for
    # individual models, so a random pick fails ~half the time. Rotating
    # through the list keeps a transient 503 from failing the whole batch
    # (which would burn a retry and eventually skip the articles).
    order = list(range(len(_MODELS)))
    random.shuffle(order)
    for idx in order:
        model = _MODELS[idx]
        key = _KEYS[idx % len(_KEYS)]
        base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")

        lang = os.environ.get("TARGET_LANG", "en")
        lines = []
        lines.append(f"你是新闻改写编辑。请把下面的文章改写成{lang}。")
        lines.append(f"重要：无论原标题或摘要是什么语言，改写后的标题(title_lang)和正文(body)必须全部使用{lang}。")
        lines.append("要求：")
        lines.append("- 基于标题和摘要，独立成文，不要逐句复制原文")
        lines.append("- 写 600-1000 字的完整文章，不要压缩成摘要")
        lines.append("- 使用多段落，段落之间用空行分隔")
        lines.append("- 保持事实准确，语气中立")
        lines.append("- 每个文章末尾加上：Sources: <source name in English>")
        lines.append("- 只输出 JSON 数组，元素格式: {article_id: <int>, title_lang: <英文标题>, body: <英文正文>}，不要其他文字。")
        lines.append("- body 中不要包含未转义的双引号，如需引用请用单引号")
        lines.append("- 如果原标题不是英文，请翻译成英文作为 title_lang")
        lines.append("")

        for art in articles:
            lines.append(f"### 文章 {art['id']} ###")
            lines.append(f"标题: {art['title']}")
            lines.append(f"摘要: {art['summary']}")
            if art.get('url'):
                lines.append(f"来源: {art['src']}")
            lines.append("")

        prompt = "\n".join(lines)

        headers = {}
        if "generativelanguage.googleapis.com" in base:
            url = f"{base}/models/{model}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": 16384,
                    "temperature": 0.4
                }
            }
            params = {"key": key}
        else:
            url = f"{base}/chat/completions"
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
            headers = {"Authorization": f"Bearer {key}"}
            params = None

        try:
            r = client.post(url, params=params, json=payload, headers=headers, timeout=300)
            if r.status_code == 503:
                last_err = f"{model}: 503 high demand"
                continue
            r.raise_for_status()
            data = r.json()
            if "generativelanguage.googleapis.com" in base:
                candidates = data.get("candidates") or []
                if not candidates:
                    last_err = f"{model}: empty LLM output (no candidates)"
                    continue
                parts_list = candidates[0].get("content", {}).get("parts", [])
                if not parts_list:
                    last_err = f"{model}: empty LLM output (no parts)"
                    continue
                text = parts_list[0].get("text", "").strip()
                if not text:
                    last_err = f"{model}: empty LLM output (blank text)"
                    continue
            else:
                choices = data.get("choices") or []
                if not choices:
                    last_err = f"{model}: empty LLM output (no choices)"
                    continue
                text = choices[0].get("message", {}).get("content", "").strip()
                if not text:
                    last_err = f"{model}: empty LLM output (blank content)"
                    continue
            result = parse_batch_output(text, len(articles))
            # A blank body means the model gave up mid-batch; try another model.
            if any(not b.strip() for _, b in result.values()):
                last_err = f"{model}: blank body in batch output"
                continue
            return result
        except Exception as e:
            last_err = f"{model}: {type(e).__name__}: {e}"
            continue

    raise RuntimeError(f"all models failed: {last_err}")


def parse_batch_output(text, expected_count):
    items = {}
    start = text.find('[')
    end = text.rfind(']')
    if start != -1 and end != -1:
        json_text = text[start:end + 1]
        try:
            data = json.loads(json_text)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        aid = item.get('article_id')
                        if aid is not None:
                            items[str(aid)] = (
                                item.get('title_lang', ''),
                                item.get('body', ''),
                            )
                if len(items) == expected_count:
                    return items
        except json.JSONDecodeError:
            # 如果 JSON 解析失败，尝试用正则提取
            import re
            # 匹配 article_id
            for m in re.finditer(r'"article_id":\s*(\d+)', json_text):
                aid = m.group(1)
                # 提取 title_lang
                title_match = re.search(r'"title_lang":\s*"((?:[^"\\]|\\.)*)"', json_text[m.start():])
                body_match = re.search(r'"body":\s*"((?:[^"\\]|\\.)*)"', json_text[m.start():])
                if title_match and body_match:
                    title_lang = title_match.group(1).replace('\\n', '\n').replace('\\"', '"')
                    body = body_match.group(1).replace('\\n', '\n').replace('\\"', '"')
                    items[aid] = (title_lang, body)
            if len(items) == expected_count:
                return items
    raise ValueError(f"Failed to parse batch output: {text[:500]}")


def main():
    c = get_conn()
    client = httpx.Client()
    log(f"rewrite worker start (batch={BATCH} sleep={SLEEP} idle={IDLE})")
    # 记录每篇文章的失败次数,避免单篇文章卡住队列
    failed_tracker = {}
    MAX_FAILURES = 3
    # 指数退避：batch 失败时逐步拉长重试间隔
    backoff = SLEEP
    MAX_BACKOFF = 300
    # 减少 build 频率：累积 N 篇或间隔 X 秒再 publish+build
    last_build_time = 0
    pending_build = 0
    BUILD_BATCH = 50
    BUILD_INTERVAL = 1800
    while True:
        rows = c.execute(
            "SELECT id, title, summary, source_name, source_url FROM articles "
            "WHERE status=0 AND summary IS NOT NULL AND length(summary) > 40 "
            "ORDER BY published_at DESC LIMIT 1000"
        ).fetchall()
        if not rows:
            failed_tracker = {}
            time.sleep(IDLE)
            continue

        # Build todo items for this batch
        todo = []
        for aid, title, summary, src, url in rows:
            if failed_tracker.get(aid, 0) >= MAX_FAILURES:
                continue
            if len(todo) >= BATCH:
                break
            todo.append({
                'id': aid,
                'title': title,
                'summary': summary,
                'src': src or "the original publication",
                'url': url,
            })
        if not todo:
            time.sleep(SLEEP)
            continue

        done = 0
        processed_ids = set()

        # Try batch rewrite
        try:
            results = batch_llm_rewrite(client, todo)
            backoff = SLEEP  # 成功，重置退避
            for aid_str, (title_lang, body) in results.items():
                aid = int(aid_str)
                if not title_lang or not body:
                    continue
                c.execute(
                    'UPDATE articles SET body=?, title_lang=?, status=1, mode="llm_rewrite", updated_at=? WHERE id=?',
                    (body, title_lang, utcnow(), aid),
                )
                c.commit()
                log(f"id={aid} rewritten ({len(body)} chars) title={title_lang[:50]}")
                processed_ids.add(aid)
                failed_tracker.pop(aid, None)
                done += 1
                pending_build += 1
        except Exception as e:
            log(f"batch rewrite FAIL: {e}")
            # 整批失败，记录待重试；不做单篇降级
            for art in todo:
                aid = art['id']
                failed_tracker[aid] = failed_tracker.get(aid, 0) + 1
                if failed_tracker[aid] >= MAX_FAILURES:
                    log(f"id={aid} failed {failed_tracker[aid]} times, will skip")
            backoff = min(backoff * 2, MAX_BACKOFF)
            log(f"backoff to {backoff}s")
            time.sleep(backoff)
            continue

        # Publish and build if enough articles accumulated or interval elapsed
        if done and (pending_build >= BUILD_BATCH or time.time() - last_build_time >= BUILD_INTERVAL):
            try:
                r = subprocess.run(
                    [sys.executable, os.path.join(BASE, "collector", "publish.py"), "--limit", "200"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                log(f"publish rc={r.returncode} {(r.stdout or '').strip()[:80]}")
            except Exception as e:
                log(f"publish FAIL: {e}")
            try:
                # start_new_session: build.sh gets its own process group so a
                # timeout can kill the whole tree (flock -> bash -> hugo ->
                # rsync). Otherwise only flock dies and hugo keeps running as
                # an orphan, holding CPU and the staging directory.
                p = subprocess.Popen(
                    [os.path.join(BASE, "collector", "build.sh")],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                try:
                    out, _ = p.communicate(timeout=3600)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        pass
                    out, _ = p.communicate()
                    p.returncode = -9
                log(f"build rc={p.returncode} {(out or '').strip()[-120:]}")
            except Exception as e:
                log(f"build FAIL: {e}")
            # 无论成功或超时都重置节流，避免构建风暴
            last_build_time = time.time()
            pending_build = 0

        # Sleep between batches
        time.sleep(backoff)


if __name__ == "__main__":
    main()
