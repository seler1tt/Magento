"""Веб-сервис для загрузки видео и поиска его URL по названию компании."""
import mimetypes
import os
import re
import tempfile
import time
from urllib.parse import quote

import psycopg2
from flask import Flask, abort, g, jsonify, request, send_from_directory
from psycopg2.extras import RealDictCursor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

DB_CONNECT_TIMEOUT_S = 10
# Сколько ждать готовности базы при старте: 30 попыток по 2 с.
DB_WAIT_ATTEMPTS = 30
DB_WAIT_DELAY_S = 2
MAX_UPLOAD_BYTES = 1024 ** 3
DEFAULT_EXTENSION = ".webm"
VIDEO_EXTENSIONS = {".webm", ".mp4", ".m4v", ".mkv", ".mov", ".avi", ".ogv"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           SERIAL      PRIMARY KEY,
    company_name TEXT        NOT NULL UNIQUE,
    file_path    TEXT        NOT NULL,
    video_url    TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# В Python до 3.11 и в части реестров Windows для .webm нет типа: файл отдавался бы как
# application/octet-stream, и Chromium скачивал бы его вместо воспроизведения.
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/mp4", ".mp4")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def _connect():
    # Параметры подключения приходят только из окружения (docker-compose.yml / .env).
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=DB_CONNECT_TIMEOUT_S,
        cursor_factory=RealDictCursor,
    )


def _wait_for_db():
    """healthcheck на pg_isready может пройти раньше, чем PostgreSQL начнёт принимать TCP-подключения
    (при первом запуске образ postgres поднимает временный сервер только на unix-сокете), поэтому повторяем."""
    for attempt in range(1, DB_WAIT_ATTEMPTS + 1):
        try:
            return _connect()
        except psycopg2.OperationalError as exc:
            if attempt == DB_WAIT_ATTEMPTS:
                raise
            app.logger.warning("Database is not ready (%s), retrying in %s s", exc, DB_WAIT_DELAY_S)
            time.sleep(DB_WAIT_DELAY_S)


def init_storage():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = _wait_for_db()
    try:
        with conn, conn.cursor() as cur:
            # Схему одновременно создают все процессы mod_wsgi, а CREATE TABLE IF NOT EXISTS
            # в PostgreSQL при гонке падает с UniqueViolation: блокировка до конца транзакции.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('videos_schema'))")
            cur.execute(SCHEMA)
    finally:
        conn.close()


def get_db():
    # psycopg2-соединение нельзя делить между потоками, поэтому у каждого запроса своё.
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _error(status, message):
    response = jsonify({"error": message})
    response.status_code = status
    return response


@app.errorhandler(404)
def not_found(exc):
    return _error(404, "Not found")


@app.errorhandler(413)
def too_large(exc):
    return _error(413, f"File is larger than {MAX_UPLOAD_BYTES} bytes")


def _base_filename(company_name):
    """Название компании становится частью имени файла и URL: оставляем только безопасные символы."""
    cleaned = re.sub(r"[^\w.-]+", "_", company_name, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")[:150].strip("._-")
    if not cleaned:
        return "video"
    if re.fullmatch(r"(?i)con|prn|aux|nul|com\d|lpt\d", cleaned):
        return f"_{cleaned}"
    return cleaned


def _extension(filename):
    ext = os.path.splitext(filename or "")[1].lower()
    return ext or DEFAULT_EXTENSION


def _pick_filename(cur, company_name, ext):
    """"A/B" и "A B" дают одно имя файла: чужой файл не перезаписываем, а добавляем суффикс."""
    base = _base_filename(company_name)
    n = 1
    while True:
        name = f"{base}{ext}" if n == 1 else f"{base}_{n}{ext}"
        cur.execute(
            "SELECT company_name FROM videos WHERE file_path = %s",
            (os.path.join(UPLOAD_DIR, name),),
        )
        owner = cur.fetchone()
        if owner is None or owner["company_name"] == company_name:
            return name
        n += 1


def _video_url(filename):
    # host_url берётся из запроса, поэтому URL совпадает с адресом, по которому к сервису обратился клиент.
    return f"{request.host_url}media/{quote(filename)}"


@app.post("/upload")
def upload_video():
    """UploadVideo: multipart/form-data с полями file и company_name."""
    company_name = (request.form.get("company_name") or "").strip()
    file = request.files.get("file")
    if not company_name:
        return _error(400, "company_name is required")
    if file is None or not file.filename:
        return _error(400, "file is required")
    ext = _extension(file.filename)
    if ext not in VIDEO_EXTENSIONS:
        return _error(400, f"Unsupported file type {ext!r}")

    # Файл сначала целиком пишется во временный: блокировка базы не держится, пока идёт загрузка,
    # а оборванная загрузка не портит уже сохранённое видео.
    fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_DIR, suffix=".part")
    os.close(fd)
    try:
        file.save(tmp_path)
        # with conn: COMMIT при успехе, ROLLBACK при исключении.
        with get_db() as conn, conn.cursor() as cur:
            # EXCLUSIVE не мешает чтению, но выстраивает загрузки в очередь:
            # иначе две параллельные загрузки могли бы выбрать одно и то же имя файла.
            cur.execute("LOCK TABLE videos IN EXCLUSIVE MODE")
            cur.execute("SELECT file_path FROM videos WHERE company_name = %s", (company_name,))
            previous = cur.fetchone()
            filename = _pick_filename(cur, company_name, ext)
            file_path = os.path.join(UPLOAD_DIR, filename)
            video_url = _video_url(filename)
            os.replace(tmp_path, file_path)
            cur.execute(
                """
                INSERT INTO videos (company_name, file_path, video_url) VALUES (%s, %s, %s)
                ON CONFLICT (company_name) DO UPDATE SET
                    file_path  = EXCLUDED.file_path,
                    video_url  = EXCLUDED.video_url,
                    updated_at = NOW()
                """,
                (company_name, file_path, video_url),
            )
    except OSError as exc:
        # Например, в Windows файл нельзя заменить, пока его кто-то воспроизводит.
        app.logger.exception("Upload failed for %r", company_name)
        return _error(500, f"Could not store the file: {exc.strerror or exc}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # Повторная загрузка с другим расширением оставила бы старый файл сиротой.
    if previous is not None and previous["file_path"] != file_path:
        try:
            os.remove(previous["file_path"])
        except OSError:
            pass

    return jsonify({"company_name": company_name, "video_url": video_url})


@app.get("/get_video_url")
def get_video_url():
    """GetVideoURL: ?company_name=<name>."""
    company_name = (request.args.get("company_name") or "").strip()
    if not company_name:
        return _error(400, "company_name is required")
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT company_name, video_url FROM videos WHERE company_name = %s", (company_name,)
        )
        row = cur.fetchone()
    if row is None:
        return _error(404, f"Company {company_name!r} not found")
    return jsonify({"company_name": row["company_name"], "video_url": row["video_url"]})


@app.get("/media/<path:filename>")
def media(filename):
    # Под Apache /media/ отдаёт сам Apache (Alias в apache.conf) и сюда запрос не доходит;
    # маршрут нужен при запуске через python web_service.py.
    # Незавершённые загрузки лежат в той же папке, но отдавать их не нужно.
    if filename.endswith(".part"):
        abort(404)
    # send_from_directory не выпускает за пределы папки и поддерживает Range: видео можно перематывать.
    return send_from_directory(UPLOAD_DIR, filename)


@app.get("/videos")
def list_videos():
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT company_name, file_path, video_url, created_at, updated_at"
            " FROM videos ORDER BY company_name"
        )
        rows = cur.fetchall()
    return jsonify(rows)


def main():
    init_storage()
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
