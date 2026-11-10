#!/bin/bash
# Serialized hugo build. Builds into a staging directory first, then rsyncs
# atomically to /var/www/hotnews so the live site stays fully available during
# the (long) build. If hugo fails, the live site keeps the previous version.
# All build triggers (cron pipeline, workers, manual) must go through this script.
SRC_DIR="$(cd "$(dirname "$(dirname "$0")")" && pwd)/site"
STAGE_DIR="$(dirname "$SRC_DIR")/build-output"
LIVE_DIR=/var/www/hotnews

exec flock -w 1200 /tmp/hotnews-build.lock bash -c "
  set -e
  rm -rf '$STAGE_DIR'
  mkdir -p '$STAGE_DIR'
  hugo --source '$SRC_DIR' --destination '$STAGE_DIR'
  rsync -a --delete '$STAGE_DIR/' '$LIVE_DIR/'
  rm -rf '$STAGE_DIR'
  echo \"build ok: \$(date -u +%FT%TZ)\"
"
