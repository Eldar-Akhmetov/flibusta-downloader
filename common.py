"""Shared settings and SQLite job queue."""
import logging
import os
import sqlite3
import sys
from pathlib import Path

JOBS_DB = Path(os.getenv("JOBS_DB", "/state/jobs.sqlite3")).resolve()
FB2_QUEUE_DIR = Path(os.getenv("FB2_QUEUE_DIR", "/work/fb2_queue")).resolve()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/downloads")).resolve()
OUTPUT_TXT_DIR = Path(os.getenv("OUTPUT_TXT_DIR", "/output")).resolve()
DELETE_FB2 = os.getenv("DELETE_FB2", "false").lower() in ("1", "true", "yes", "on")

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger("flibusta")


def init_db() -> None:
    JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(JOBS_DB), timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("""CREATE TABLE IF NOT EXISTS topics (
            topic_url   TEXT PRIMARY KEY,
            status      TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            error       TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_url   TEXT NOT NULL,
            src_dir     TEXT NOT NULL,
            dst_dir     TEXT NOT NULL,
            status      TEXT NOT NULL,
            worker      TEXT,
            attempts    INTEGER NOT NULL DEFAULT 0,
            error       TEXT,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id)")
        db.commit()


def requeue_stale(stale_seconds: int) -> int:
    """Вернуть в pending задачи, зависшие в running дольше stale_seconds."""
    with sqlite3.connect(str(JOBS_DB), timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        cur = db.execute(
            """UPDATE jobs
               SET status='pending', worker=NULL, updated_at=CURRENT_TIMESTAMP
               WHERE status='running'
                 AND updated_at < datetime('now', ?)""",
            (f"-{int(stale_seconds)} seconds",),
        )
        db.commit()
        return cur.rowcount