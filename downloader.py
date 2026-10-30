#!/usr/bin/env python3
"""Scan forum -> torrent -> unzip -> enqueue FB2 for conversion."""
import hashlib
import os
import re
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

import libtorrent as lt
import requests
from bs4 import BeautifulSoup

from common import DOWNLOAD_DIR, FB2_QUEUE_DIR, JOBS_DB, LOG, HTTP_TIMEOUT, init_db

FORUM_URL = os.getenv("FORUM_URL", "https://booktracker.org/viewforum.php?f=255")
COOKIE = os.getenv("COOKIE", "").strip()
POLL_INTERVAL = float(os.getenv("DOWNLOADER_POLL_INTERVAL", "0"))
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "6881"))
PAGE_DELAY = float(os.getenv("PAGE_DELAY", "2"))
METADATA_TIMEOUT = float(os.getenv("METADATA_TIMEOUT", "180"))
TORRENT_TIMEOUT = float(os.getenv("TORRENT_TIMEOUT", str(24 * 3600)))
DELETE_TORRENT_AFTER_QUEUE = os.getenv("DELETE_TORRENT_AFTER_QUEUE", "false").lower() in ("1", "true", "yes", "on")
JOBS_PER_BATCH = int(os.getenv("JOBS_PER_BATCH", "200"))

STEP = 25
USER_AGENT = "Mozilla/5.0 (compatible; FlibustaArchiver/1.0)"

TOPIC_RE = re.compile(r'viewtopic\.php\?[^"\']*?\bt=(\d+)')
MAGNET_RE = re.compile(r'(magnet:\?xt=urn:btih:[^"\'<>\s]+)', re.IGNORECASE)
TORRENT_FILE_RE = re.compile(
    r'(download/file\.php\?id=\d+|dl\.php\?[^"\']+\.torrent|[^"\']+\.torrent)',
    re.IGNORECASE,
)
COMPLETE_MARKER = ".download_complete"


def _headers() -> dict:
    h = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    if COOKIE:
        h["Cookie"] = COOKIE
    return h


def fetch(url: str) -> str:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_headers(), timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.text
        except requests.RequestException as exc:
            last = exc
            LOG.warning("fetch %s attempt %d/3: %s", url, attempt + 1, exc)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"fetch failed: {url}: {last}")


def extract_topic_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen, urls = set(), []
    for a in soup.find_all("a", href=TOPIC_RE):
        href = a.get("href")
        if not href:
            continue
        full = requests.compat.urljoin("https://booktracker.org/", href).split("#")[0]
        if full not in seen:
            seen.add(full)
            urls.append(full)
    return urls


def has_next_page(html: str, current_start: int) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    next_start = current_start + STEP
    pattern = re.compile(rf'start={next_start}\b')
    for a in soup.find_all("a", href=pattern):
        return True
    for a in soup.find_all("a"):
        text = (a.get_text() or "").strip().lower()
        if text in ("след.", "след", "next", ">", "»"):
            return True
    return False


def scan_forum(forum_url: str) -> list[str]:
    all_urls, seen, start = [], set(), 0
    while True:
        url = f"{forum_url}&start={start}"
        LOG.info("forum page: %s", url)
        try:
            html = fetch(url)
        except RuntimeError as exc:
            LOG.error("%s", exc)
            break
        page_urls = extract_topic_links(html)
        if not page_urls:
            break
        for u in page_urls:
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
        LOG.info("start=%d: total topics %d", start, len(all_urls))
        if not has_next_page(html, start):
            break
        start += STEP
        time.sleep(PAGE_DELAY)
    return all_urls


def extract_magnet(topic_url: str) -> str | None:
    html = fetch(topic_url)
    matches = MAGNET_RE.findall(html)
    if not matches:
        return None
    matches.sort(key=lambda m: m.count("&tr="), reverse=True)
    return matches[0]


def extract_torrent_file_url(topic_url: str) -> str | None:
    html = fetch(topic_url)
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=TORRENT_FILE_RE):
        href = a.get("href")
        if not href:
            continue
        if (".torrent" in href.lower()
                or "download/file.php" in href.lower()
                or "dl.php" in href.lower()):
            return requests.compat.urljoin("https://booktracker.org/", href)
    return None


class Downloader:
    def __init__(self, listen_port: int):
        self.session = lt.session({"listen_interfaces": f"0.0.0.0:{listen_port}"})

    def download_magnet(self, magnet: str, save_dir: Path) -> None:
        save_dir.mkdir(parents=True, exist_ok=True)
        handle = lt.add_magnet_uri(self.session, magnet, {"save_path": str(save_dir)})
        self._wait(handle)

    def download_torrent_file(self, torrent_url: str, save_dir: Path) -> None:
        save_dir.mkdir(parents=True, exist_ok=True)
        r = requests.get(torrent_url, headers=_headers(), timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        info = lt.torrent_info(lt.bdecode(r.content))
        handle = self.session.add_torrent({"ti": info, "save_path": str(save_dir)})
        self._wait(handle)

    def _wait(self, handle) -> None:
        try:
            t0 = time.time()
            while not handle.has_metadata():
                if time.time() - t0 > METADATA_TIMEOUT:
                    raise TimeoutError("metadata timeout")
                time.sleep(1)
            LOG.info("metadata: %s", handle.status().name)

            t0 = time.time()
            next_log = 0.0
            while True:
                s = handle.status()
                if s.is_seeding or s.progress >= 1.0:
                    break
                if time.time() - t0 > TORRENT_TIMEOUT:
                    raise TimeoutError("torrent timeout")
                if time.time() - t0 > next_log:
                    LOG.info("  %.1f%% peers=%d down=%.0f KB/s",
                             s.progress * 100, s.num_peers, s.download_rate / 1024)
                    next_log = time.time() - t0 + 30
                time.sleep(5)
        finally:
            try:
                self.session.remove_torrent(handle)
            except Exception:
                pass

    def close(self):
        try:
            self.session.pause()
        finally:
            del self.session


def _safe_extract(zip_path: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError:
                LOG.warning("unsafe path in %s: %s", zip_path.name, info.filename)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def extract_zip_tree(src: Path) -> int:
    total = 0
    while True:
        zips = [p for p in src.rglob("*") if p.is_file() and p.suffix.lower() == ".zip"]
        if not zips:
            break
        for z in zips:
            target = src / "_extracted" / z.relative_to(src).with_suffix("")
            try:
                n = _safe_extract(z, target)
            except (zipfile.BadZipFile, OSError) as exc:
                LOG.error("unzip failed %s: %s", z.name, exc)
                continue
            total += n
            LOG.info("extracted %s -> %d files", z.name, n)
            z.unlink(missing_ok=True)
    return total


def enqueue_jobs_batch(topic_url: str, batches: list[tuple[Path, Path]]) -> None:
    """Атомарно вставить несколько job'ов и отметить тему как downloaded."""
    with sqlite3.connect(str(JOBS_DB), timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN IMMEDIATE")
        for src_dir, dst_dir in batches:
            db.execute(
                """INSERT INTO jobs(topic_url, src_dir, dst_dir, status)
                   VALUES(?, ?, ?, 'pending')""",
                (topic_url, str(src_dir), str(dst_dir)),
            )
        db.execute(
            """INSERT INTO topics(topic_url, status, updated_at)
               VALUES(?, 'downloaded', CURRENT_TIMESTAMP)
               ON CONFLICT(topic_url) DO UPDATE SET
                 status='downloaded',
                 error=NULL,
                 updated_at=CURRENT_TIMESTAMP""",
            (topic_url,),
        )
        db.commit()


def process_topic(downloader: Downloader, topic_url: str) -> None:
    folder = hashlib.sha256(topic_url.encode()).hexdigest()[:16]
    torrent_dir = DOWNLOAD_DIR / folder
    queue_dir = FB2_QUEUE_DIR / folder
    out_root = Path("/output") / folder
    marker = torrent_dir / COMPLETE_MARKER

    if queue_dir.exists() and any(queue_dir.rglob("*.fb2")):
        with sqlite3.connect(str(JOBS_DB), timeout=30) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE topic_url=?", (topic_url,)
            ).fetchone()
        if row and row[0] > 0:
            LOG.info("already enqueued %s (%d jobs)", topic_url, row[0])
            return

    if not marker.exists():
        magnet = extract_magnet(topic_url)
        if magnet:
            if torrent_dir.exists():
                shutil.rmtree(torrent_dir)
            downloader.download_magnet(magnet, torrent_dir)
        else:
            turl = extract_torrent_file_url(topic_url)
            if not turl:
                raise RuntimeError("no magnet and no .torrent link on topic page")
            if torrent_dir.exists():
                shutil.rmtree(torrent_dir)
            downloader.download_torrent_file(turl, torrent_dir)

        has_fb2 = any(p.suffix.lower() == ".fb2" for p in torrent_dir.rglob("*"))
        has_zip = any(p.suffix.lower() == ".zip" for p in torrent_dir.rglob("*"))
        if not (has_fb2 or has_zip):
            raise RuntimeError("no FB2 or ZIP files after download")
        marker.touch()

    extract_zip_tree(torrent_dir)

    fb2_files = sorted(
        p for p in torrent_dir.rglob("*")
        if p.is_file() and p.suffix.lower() == ".fb2"
    )
    if not fb2_files:
        raise RuntimeError("no FB2 files after extraction")

    if queue_dir.exists():
        shutil.rmtree(queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)

    batches: list[tuple[Path, Path]] = []
    for batch_idx, start in enumerate(range(0, len(fb2_files), JOBS_PER_BATCH)):
        chunk = fb2_files[start:start + JOBS_PER_BATCH]
        batch_dir = queue_dir / f"part_{batch_idx:04d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        for fb2 in chunk:
            # Плоское имя: в разных подпапках торрента могут быть одноимённые файлы.
            # Уникальность в пределах part_NNNN даёт хэш относительного пути.
            rel = fb2.relative_to(torrent_dir)
            uniq = hashlib.sha256(str(rel).encode()).hexdigest()[:12]
            dst = batch_dir / f"{uniq}__{fb2.name}"
            shutil.copy2(fb2, dst)
        # Выходная папка батча: /output/<hash>/part_NNNN
        out_dir = out_root / f"part_{batch_idx:04d}"
        batches.append((batch_dir, out_dir))

    enqueue_jobs_batch(topic_url, batches)
    LOG.info("enqueued %d jobs (%d FB2 files) from %s",
             len(batches), len(fb2_files), topic_url)

    if DELETE_TORRENT_AFTER_QUEUE:
        shutil.rmtree(torrent_dir, ignore_errors=True)
        LOG.info("removed torrent dir %s", torrent_dir)


def run_scan(downloader: Downloader) -> int:
    init_db()
    topic_urls = scan_forum(FORUM_URL)
    LOG.info("topic URLs collected: %d", len(topic_urls))

    with sqlite3.connect(str(JOBS_DB), timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        known = {row[0] for row in db.execute(
            "SELECT topic_url FROM topics WHERE status IN ('downloaded','failed_download')"
        ).fetchall()}

    processed = 0
    for link in topic_urls:
        if link in known:
            continue
        try:
            process_topic(downloader, link)
            processed += 1
        except Exception as exc:
            LOG.error("failed %s: %s", link, exc)
            with sqlite3.connect(str(JOBS_DB), timeout=30) as db:
                db.execute("PRAGMA busy_timeout=30000")
                db.execute(
                    """INSERT INTO topics(topic_url, status, error, updated_at)
                       VALUES(?, 'failed_download', ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(topic_url) DO UPDATE SET
                         status='failed_download',
                         error=excluded.error,
                         updated_at=CURRENT_TIMESTAMP""",
                    (link, str(exc)),
                )
                db.commit()
    return processed


def main() -> int:
    init_db()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    FB2_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    d = Downloader(LISTEN_PORT)
    try:
        if POLL_INTERVAL <= 0:
            n = run_scan(d)
            LOG.info("done, new topics downloaded: %d", n)
            return 0
        while True:
            try:
                n = run_scan(d)
                LOG.info("cycle done, new topics: %d", n)
            except Exception as exc:
                LOG.error("scan cycle failed: %s", exc)
            LOG.info("sleeping %.0fs", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)
    finally:
        d.close()


if __name__ == "__main__":
    raise SystemExit(main())