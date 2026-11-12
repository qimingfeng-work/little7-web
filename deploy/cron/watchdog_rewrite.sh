#!/bin/bash
# Watchdog for hotnews-rewrite.
#
# The rewrite worker writes a log line for every rewrite / publish / build
# result. A healthy worker always logs within one build cycle (~40 min worst
# case). If the log has been silent for MAX_AGE seconds the worker is almost
# certainly wedged (observed once: a single time.sleep() that never returned,
# leaving the worker idle for 6+ hours while articles piled up).
#
# Recovery: SIGKILL the main PID; systemd (Restart=on-failure) brings it back.
# Killing is safe: the worker only holds an idle SQLite connection and an
# httpx connection pool at that point, and the DB is in WAL mode.

SVC=hotnews-rewrite
LOG=/home/ubuntu/hotnews/logs/rewrite_worker.log
MAX_AGE=$((90 * 60))   # 90 min > worst-case build (~40 min)

[ -f "$LOG" ] || exit 0

now=$(date +%s)
mtime=$(stat -c %Y "$LOG")
age=$((now - mtime))

if [ "$age" -lt "$MAX_AGE" ]; then
    exit 0
fi

pid=$(systemctl show "$SVC" -p MainPID --value 2>/dev/null)
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    exit 0
fi

echo "$(date -u +%FT%TZ) watchdog: ${SVC} silent for ${age}s (pid $pid), killing" \
    >> /home/ubuntu/hotnews/logs/watchdog.log
kill -9 "$pid"
