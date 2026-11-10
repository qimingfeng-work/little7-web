# HotNews Daily

News aggregator: RSS collection → LLM rewrite → multi-language translation → Hugo static site → nginx.

## Pipeline

```
RSS feeds (sources.json)
   │  collect.py / collect_worker.py        → SQLite (articles, status=0)
   │  rewrite_worker.py                    → LLM rewrite to English (status=1)
   │  publish.py                           → site/content/**/*.md (status=2)
   │  build.sh                             → Hugo build → rsync /var/www/hotnews
   │  translate_worker.py / _fr.py         → .<lang>.md next to the English file
```

## Repository layout

```
collector/          Python pipeline (collect, rewrite, translate, publish, build)
  sources.json      RSS feed list
  build.sh          Hugo build + atomic rsync to /var/www/hotnews
site/               Hugo site
  hugo.toml
  layouts/          Templates (baseof / single / list / rss)
  static/           CSS, logo, favicons
deploy/
  systemd/          Service units for the 4 workers
  nginx/            nginx vhost (HTTP→HTTPS, /zh→/zh-tw, goaccess)
  cron/gen_goaccess.sh   Access-log report generator
```

Not versioned: `collector/.env` (API keys), `hotnews.db` (runtime data),
`site/content/` (generated markdown), `venv/`, build output.

## Server deployment

```bash
# 1. Code
git clone <repo> /home/ubuntu/hotnews
cd /home/ubuntu/hotnews

# 2. Python env
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 3. Secrets
cp .env.example collector/.env      # then edit and fill in the API keys
chmod 600 collector/.env

# 4. Database (created automatically on first run)
venv/bin/python collector/collect.py

# 5. Services
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hotnews-collect hotnews-rewrite hotnews-translate hotnews-translate-fr

# 6. nginx
sudo cp deploy/nginx/hotnews /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 7. GoAccess report (every 30 min)
( crontab -l ; echo '*/30 * * * * /home/ubuntu/hotnews/deploy/cron/gen_goaccess.sh' ) | crontab -
```

## Configuration

Environment variables live in `collector/.env` (see `.env.example`).
Key settings:

| Variable | Meaning |
|---|---|
| `TARGET_LANG` | Rewrite output language (fixed to `en`) |
| `LLM_BASE_URL` / `LLM_MODELS` | LLM endpoint and model pool (random pick per call) |
| `COLLECT_INTERVAL` | Seconds between RSS runs |
| `REWRITE_BATCH` / `REWRITE_SLEEP` | Rewrite batch size and inter-batch sleep |
| `TRANSLATE_BATCH` / `TRANSLATE_SLEEP` | Translation batch size and inter-batch sleep |
| `TRANSLATE_FR_*` | French worker (separate key) |

RSS sources are listed in `collector/sources.json`; each entry sets
`name`, `url`, `lang` and whether to split `Title - Source`.

## Operations

```bash
# Manual collection / rebuild
venv/bin/python collector/collect.py
bash collector/build.sh

# Worker status and logs
systemctl status hotnews-collect hotnews-rewrite hotnews-translate hotnews-translate-fr
journalctl -u hotnews-rewrite -f

# Logs
logs/collect_worker.log  logs/rewrite_worker.log  logs/translate_worker.log
```

Build lock: `build.sh` serialises builds with `flock /tmp/hotnews-build.lock`.
Do not delete that file — removing it breaks mutual exclusion between the
manual build and the rewrite worker's automatic builds.
