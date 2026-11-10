#!/usr/bin/env python3
"""
Background collection worker.

Continuously runs:
    venv/bin/python collector/collect.py

Usage:
    python3 collector/collect_worker.py
"""

import logging
import os
import subprocess
import sys
import time

BASE = "/home/ubuntu/hotnews"
LOGS = os.path.join(BASE, "logs")
COLLECT_SCRIPT = os.path.join(BASE, "collector", "collect.py")
LOG_FILE = os.path.join(LOGS, "collect_worker.log")

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("collect_worker")

# Pull interval from environment, default 300 seconds (5 minutes)
INTERVAL = int(os.environ.get("COLLECT_INTERVAL", "300"))


def run_collect():
    logger.info("Starting collect.py run")
    try:
        result = subprocess.run(
            [sys.executable, COLLECT_SCRIPT],
            cwd=BASE,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0:
            logger.info("Collect completed successfully")
            if result.stdout.strip():
                logger.info("collect output: %s", result.stdout.strip()[:500])
        else:
            logger.error(
                "Collect failed with rc=%s. stderr: %s",
                result.returncode,
                result.stderr.strip()[:2000],
            )
    except subprocess.TimeoutExpired:
        logger.error("Collect timed out after 600 seconds")
    except Exception as e:
        logger.exception("Unexpected error during collect: %s", e)


def main():
    logger.info("collect_worker started")
    try:
        while True:
            run_collect()
            logger.info("Sleeping for %s seconds", INTERVAL)
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        logger.info("collect_worker stopped by user")
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        raise


if __name__ == "__main__":
    main()
