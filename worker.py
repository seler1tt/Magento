"""Фоновый обработчик: берёт из PostgreSQL загруженные видео, рендерит zoom через apply_zoom.py
и сохраняет URL результата (processed_video_url) и статус обработки."""
import logging
import os
import tempfile
import time
from urllib.parse import quote

import apply_zoom
from web_service import PROCESSED_DIR, _wait_for_db, init_storage

POLL_INTERVAL_S = 2
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

log = logging.getLogger("worker")


def _reset_interrupted(conn):
    # Worker один: запись в processing осталась от прерванного запуска, обработаем её заново.
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE videos SET processing_status = 'pending' WHERE processing_status = 'processing'")


def _claim(conn):
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE videos SET processing_status = 'processing'
            WHERE id = (
                SELECT id FROM videos WHERE processing_status = 'pending'
                ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            RETURNING id, company_name, file_path, video_url, zoom_path, updated_at
            """
        )
        return cur.fetchone()


def _processed_url(video_url, filename):
    # video_url собран из адреса, по которому клиент загрузил видео: результат лежит рядом, в processed/.
    return f"{video_url.rsplit('/', 1)[0]}/processed/{quote(filename)}"


def _process(job):
    """Возвращает (status, processed_file_path, processed_video_url)."""
    if job["zoom_path"] is None:
        return "skipped", None, None
    settings = apply_zoom.load_sidecar(job["zoom_path"])
    segments = apply_zoom.segments_from(settings)
    stem = os.path.splitext(os.path.basename(job["file_path"]))[0]
    filename = stem + apply_zoom.output_extension(job["file_path"], segments)
    path = os.path.join(PROCESSED_DIR, filename)
    # Пока ffmpeg пишет файл, по URL результата отдаётся прежняя версия, а не недописанная.
    fd, tmp_path = tempfile.mkstemp(dir=PROCESSED_DIR, suffix=".part")
    os.close(fd)
    try:
        apply_zoom.render(job["file_path"], tmp_path, settings, ffmpeg=FFMPEG)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return "done", path, _processed_url(job["video_url"], filename)


def _finish(conn, job, status, path, url, error):
    with conn, conn.cursor() as cur:
        # updated_at меняется при повторной загрузке: тогда результат устарел, новая версия уже в очереди.
        cur.execute(
            """
            UPDATE videos SET
                processing_status   = %s,
                processing_error    = %s,
                processed_file_path = %s,
                processed_video_url = %s
            WHERE id = %s AND updated_at = %s
            """,
            (status, error, path, url, job["id"], job["updated_at"]),
        )
        if cur.rowcount == 0:
            log.info("%r was re-uploaded during processing, result discarded", job["company_name"])


def run():
    init_storage()
    conn = _wait_for_db()
    _reset_interrupted(conn)
    log.info("Worker started, ffmpeg: %s", FFMPEG)
    while True:
        job = _claim(conn)
        if job is None:
            time.sleep(POLL_INTERVAL_S)
            continue
        log.info("Processing %r", job["company_name"])
        try:
            status, path, url = _process(job)
            error = None
        except Exception as exc:
            log.exception("Processing failed for %r", job["company_name"])
            status, path, url, error = "failed", None, None, str(exc)
        _finish(conn, job, status, path, url, error)
        log.info("%r: %s", job["company_name"], status)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run()
