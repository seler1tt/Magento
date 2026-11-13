"""Запись zoom-событий в qscript.py: переключение in/out и sidecar."""
import json

import apply_zoom
import qscript


def _recorder(started=100.0):
    recorder = qscript._ZoomRecorder()
    recorder.video_started = started
    return recorder


def test_toggle_alternates_in_and_out():
    recorder = _recorder()
    assert recorder.toggle(640, 360, 103_000) == "in"
    assert recorder.toggle(700.4, 300.6, 108_250) == "out"
    assert recorder.toggle(10, 20, 110_000) == "in"
    assert recorder.events == [
        {"t": 3.0, "x": 640, "y": 360, "type": "in"},
        {"t": 8.25, "x": 700, "y": 301, "type": "out"},
        {"t": 10.0, "x": 10, "y": 20, "type": "in"},
    ]


def test_time_is_relative_to_video_start():
    recorder = _recorder(started=1_000.0)
    recorder.toggle(1, 1, 999_000)                      # раньше начала записи — t не отрицательное
    recorder.toggle(1, 1, 1_002_345)
    assert [e["t"] for e in recorder.events] == [0, 2.345]


def test_sidecar_written_only_with_events(tmp_path):
    video = tmp_path / "Muster GmbH.webm"
    assert _recorder().write_sidecar(str(video)) is None
    assert not (tmp_path / "Muster GmbH.zoom.json").exists()

    recorder = _recorder()
    recorder.toggle(640, 360, 103_000)
    recorder.toggle(640, 360, 108_000)
    path = recorder.write_sidecar(str(video))
    assert path == str(tmp_path / "Muster GmbH.zoom.json")
    data = json.loads((tmp_path / "Muster GmbH.zoom.json").read_text(encoding="utf-8"))
    assert data == {
        "viewport": {"width": 1280, "height": 720},
        "fps": 25,
        "zoom_factor": 2.0,
        "ramp_sec": 0.5,
        "events": [
            {"t": 3.0, "x": 640, "y": 360, "type": "in"},
            {"t": 8.0, "x": 640, "y": 360, "type": "out"},
        ],
    }
    # Sidecar читается apply_zoom.py.
    assert len(apply_zoom.segments_from(apply_zoom.load_sidecar(path))) == 1


def test_stale_sidecar_removed_when_no_events(tmp_path):
    video = tmp_path / "Muster GmbH.webm"
    stale = tmp_path / "Muster GmbH.zoom.json"
    stale.write_text("{}", encoding="utf-8")
    assert _recorder().write_sidecar(str(video)) is None
    assert not stale.exists()
