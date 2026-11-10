#!/bin/bash
# Regenerate GoAccess HTML report from nginx access logs (all rotated files).
# Runs as ubuntu (adm group can read /var/log/nginx).
set -u
OUT=/var/www/goaccess/goaccess.html
LOG=/home/ubuntu/hotnews/logs/goaccess.log
TMPDIR_LOGS=$(mktemp -d /tmp/goaccess-logs.XXXXXX)
trap 'rm -rf "$TMPDIR_LOGS"' EXIT

# Collect all log files: current + rotated (decompress .gz to temp)
LOG_FILES="/var/log/nginx/access.log /var/log/nginx/access.log.1"
for f in /var/log/nginx/access.log.*.gz; do
  [ -e "$f" ] || continue
  base=$(basename "$f" .gz)
  zcat "$f" > "$TMPDIR_LOGS/$base" 2>/dev/null || continue
  LOG_FILES="$LOG_FILES $TMPDIR_LOGS/$base"
done

if [ -z "$(echo $LOG_FILES | tr -d ' ')" ]; then
  echo "$(date -u +%FT%TZ) no access logs" >> "$LOG"
  exit 0
fi

# shellcheck disable=SC2086
goaccess $LOG_FILES --log-format=COMBINED -o "$OUT" --no-query-string >> "$LOG" 2>&1
chown ubuntu:ubuntu "$OUT" 2>/dev/null
echo "$(date -u +%FT%TZ) ok ($(stat -c %s "$OUT" 2>/dev/null) bytes)" >> "$LOG"
