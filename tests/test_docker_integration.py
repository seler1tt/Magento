"""Интеграция с Docker-стеком: ffmpeg в образе и zoom-обработка загруженного видео.

Нужен запущенный docker compose up; адрес сервиса задаётся переменной окружения:
    $env:ZOOM_SERVICE_URL = "http://127.0.0.1:8000"; pytest tests/test_docker_integration.py
"""
import json
import os
import subprocess
import time

import pytest

SERVICE_URL = os.environ.get("ZOOM_SERVICE_URL")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO = os.path.join(PROJECT_DIR, "video", "Muster GmbH.webm")
COMPANY = "Zoom integration test"
PROCESSING_TIMEOUT_S = 300
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"

pytestmark = pytest.mark.skipif(not SERVICE_URL, reason="ZOOM_SERVICE_URL is not set")


@pytest.mark.parametrize("service", ["web", "worker"])
def test_image_has_ffmpeg(service):
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", service, "ffmpeg", "-version"],
        cwd=PROJECT_DIR, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ffmpeg version")


def _wait_processed(requests):
    deadline = time.monotonic() + PROCESSING_TIMEOUT_S
    while time.monotonic() < deadline:
        response = requests.get(f"{SERVICE_URL}/videos", timeout=10)
        response.raise_for_status()
        row = next(r for r in response.json() if r["company_name"] == COMPANY)
        if row["processing_status"] not in ("pending", "processing"):
            return row
        time.sleep(2)
    pytest.fail(f"video was not processed in {PROCESSING_TIMEOUT_S} s")


def test_uploaded_video_is_zoomed():
    import requests

    sidecar = {
        "viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5,
        "events": [{"t": 2.0, "x": 640, "y": 360, "type": "in"}, {"t": 5.0, "x": 640, "y": 360, "type": "out"}],
    }
    with open(VIDEO, "rb") as f:
        original = f.read()
    response = requests.post(
        f"{SERVICE_URL}/upload",
        files={
            "file": ("test.webm", original, "video/webm"),
            "zoom": ("test.zoom.json", json.dumps(sidecar), "application/json"),
        },
        data={"company_name": COMPANY},
        timeout=60,
    )
    assert response.status_code == 200, response.text

    row = _wait_processed(requests)
    assert row["processing_status"] == "done", row["processing_error"]
    assert row["processed_video_url"].endswith(".webm")

    processed = requests.get(row["processed_video_url"], timeout=60)
    assert processed.status_code == 200
    assert processed.content.startswith(WEBM_MAGIC)
    assert processed.content != original


def test_invalid_sidecar_rejected():
    import requests

    with open(VIDEO, "rb") as f:
        response = requests.post(
            f"{SERVICE_URL}/upload",
            files={"file": ("test.webm", f, "video/webm"), "zoom": ("bad.zoom.json", b"not json", "application/json")},
            data={"company_name": COMPANY},
            timeout=60,
        )
    assert response.status_code == 400
