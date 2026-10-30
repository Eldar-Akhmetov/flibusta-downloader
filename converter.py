#!/usr/bin/env python3
"""Consume pending conversion jobs from SQLite; convert FB2 -> TXT in threads."""
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import JOBS_DB, LOG, OUTPUT_TXT_DIR, init_db, requeue_stale

WORKERS = int(os.getenv("CONVERT_WORKERS", "1"))
POLL = float(os.getenv("CONVERT_POLL_INTERVAL", "10"))
TIMEOUT = float(os.getenv("CONVERT_TIMEOUT", "600"))
STALE_SECONDS = int(os.getenv("STALE_RUNNING_SECONDS", "86400"))
DELETE_FB2 = os.getenv("DELETE_FB2", "false").lower() in ("1", "true", "yes", "on")
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"


def claim_one() -> dict | None:
    """Атомарно взять одну pending-задачу в работу."""
    with sqlite3.connect(str(JOBS_DB), timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """SELECT id, topic_url, src_dir, dst_dir, attempts
               FROM jobs WHERE status='pending'
               ORDER BY id LIMIT 1"""
        ).fetchone()
        if not row:
            db.rollback()
            return None
        db.execute(
            """UPDATE jobs
               SET status='running', worker=?, attempts=attempts+1,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status='pending'""",
            (WORKER_ID, row[0]),
        )
        db.commit()
    return {
        "id": row[0],
        "topic_url": row[1],
        "src_dir": row[2],
        "dst_dir": row[3],
        "attempts": row[4] + 1,
    }


def finish_job(job_id: int, status: str, error: str | None) -> None:
    with sqlite3.connect(str(JOBS_DB), timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute(
            """UPDATE jobs
               SET status=?, error=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (status, error, job_id),
        )
        db.commit()


def convert_one(fb2: Path, txt: Path) -> tuple[bool, str | None]:
    txt.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["ebook-convert", str(fb2), str(txt)],
            capture_output=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace").strip()
        return False, err or f"exit {result.returncode}"
    return True, None


def process_job(job: dict) -> tuple[int, int]:
    src = Path(job["src_dir"])
    dst = Path(job["dst_dir"])

    # Повторный захват: чистим dst_dir, чтобы не смешивать файлы прошлых попыток.
    if job["attempts"] > 1:
        LOG.warning("job=%s attempt=%d, cleaning %s", job["id"], job["attempts"], dst)
        if dst.exists():
            shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    fb2_files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() == ".fb2")
    if not fb2_files:
        raise RuntimeError("no FB2 in queue dir")

    ok = fail = 0
    errors = []
    for fb2 in fb2_files:
        out = dst / fb2.relative_to(src).with_suffix(".txt")
        success, err = convert_one(fb2, out)
        if success:
            ok += 1
            if DELETE_FB2:
                fb2.unlink(missing_ok=True)
        else:
            fail += 1
            errors.append(f"{fb2.name}: {err}")
            LOG.error("convert failed %s: %s", fb2.name, err)
        LOG.info("job=%s %s -> %s (%s)", job["id"], fb2.name, out.name,
                 "ok" if success else "FAIL")

    if fail:
        raise RuntimeError(f"{fail}/{len(fb2_files)} failed; first: {errors[0]}")
    return ok, fail


def main() -> int:
    init_db()
    OUTPUT_TXT_DIR.mkdir(parents=True, exist_ok=True)
    LOG.info("converter worker %s, workers=%d, poll=%.0fs, stale=%ds",
             WORKER_ID, WORKERS, POLL, STALE_SECONDS)

    requeued = requeue_stale(STALE_SECONDS)
    if requeued:
        LOG.warning("requeued %d stale running jobs on start", requeued)
    last_requeue = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        inflight: dict = {}
        while True:
            # периодическая реанимация зависших running
            if time.time() - last_requeue > 60:
                n = requeue_stale(STALE_SECONDS)
                if n:
                    LOG.warning("requeued %d stale running jobs", n)
                last_requeue = time.time()

            # добираем, пока есть свободные слоты и есть pending
            while len(inflight) < WORKERS:
                job = claim_one()
                if not job:
                    break
                fut = pool.submit(process_job, job)
                inflight[fut] = job
                LOG.info("claimed job=%s (%s) attempt=%d",
                         job["id"], job["topic_url"], job["attempts"])

            if not inflight:
                time.sleep(POLL)
                continue

            done_futs = [f for f in inflight if f.done()]
            if not done_futs:
                time.sleep(2)
                continue

            for fut in done_futs:
                job = inflight.pop(fut)
                try:
                    ok, fail = fut.result()
                    finish_job(job["id"], "done", None)
                    LOG.info("job=%s done: ok=%d", job["id"], ok)
                except Exception as exc:
                    finish_job(job["id"], "failed", str(exc))
                    LOG.error("job=%s failed: %s", job["id"], exc)


if __name__ == "__main__":
    raise SystemExit(main())