"""Клиент веб-сервиса видео: загрузка, поиск URL и просмотр в Chromium."""
import mimetypes
import os
from typing import Optional

CONNECT_TIMEOUT_S = 10
# Загрузка большого файла по медленной сети занимает время, поэтому таймаут чтения щедрый.
READ_TIMEOUT_S = 300

mimetypes.add_type("video/webm", ".webm")


def _endpoint(server_url, path):
    return f"{server_url.rstrip('/')}/{path}"


def _raise_for_status(response):
    """Как raise_for_status, но с текстом ошибки, который вернул сервис."""
    import requests

    if response.ok:
        return
    try:
        detail = response.json().get("error")
    except ValueError:
        detail = None
    raise requests.HTTPError(
        f"{response.status_code} {response.reason}: {detail or response.text[:200]}",
        response=response,
    )


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL."""
    # requests импортируется здесь, чтобы запись без --server работала и без этой библиотеки.
    import requests

    filename = os.path.basename(video_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(video_path, "rb") as f:
        response = requests.post(
            _endpoint(server_url, "upload"),
            files={"file": (filename, f, content_type)},
            data={"company_name": company_name},
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
        )
    _raise_for_status(response)
    return response.json()["video_url"]


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name."""
    import requests

    response = requests.get(
        _endpoint(server_url, "get_video_url"),
        params={"company_name": company_name},
        timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
    )
    _raise_for_status(response)
    return response.json()["video_url"]


# Ошибка загрузки тоже завершает ожидание: иначе оно длилось бы вечно.
VIDEO_FINISHED_JS = "() => { const v = document.querySelector('video'); return !!v && (v.ended || !!v.error); }"


def PreviewVideo(video_url: str, view_seconds: Optional[float] = None) -> None:
    """Открывает видео в Chromium: None — до конца видео, > 0 — на столько секунд, 0 — до нажатия Enter."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        # Без жеста пользователя Chromium может не запустить воспроизведение сам.
        browser = playwright.chromium.launch(
            headless=False, args=["--autoplay-policy=no-user-gesture-required"]
        )
        try:
            page = browser.new_page()
            # Прямая ссылка на .webm открывается во встроенном плеере Chromium.
            response = page.goto(video_url, wait_until="domcontentloaded")
            if response is not None and not response.ok:
                raise RuntimeError(f"{video_url} returned HTTP {response.status}")
            if view_seconds is None:
                page.wait_for_function(VIDEO_FINISHED_JS, polling=500, timeout=0)
            elif view_seconds > 0:
                page.wait_for_timeout(view_seconds * 1000)
            else:
                input("Press Enter to close the preview...")
        finally:
            browser.close()
