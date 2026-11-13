"""Zoom-логика apply_zoom.py без ffmpeg: выражения zoompan вычисляются в Python."""
import json

import pytest

import apply_zoom

FPS = 25
RAMP = 12  # ramp_frames(0.5, 25)
W, H = 1280, 720


def _clip(value, low, high):
    return min(max(value, low), high)


def _evaluate(expr, **variables):
    # Выражения apply_zoom записаны общим подмножеством синтаксиса ffmpeg и Python.
    names = {"clip": _clip, "min": min, "gte": lambda a, b: 1 if a >= b else 0}
    names.update(variables)
    return eval(expr, {"__builtins__": {}}, names)


def _settings(events, **overrides):
    data = {"viewport": {"width": W, "height": H}, "fps": FPS, "zoom_factor": 2.0, "ramp_sec": 0.5,
            "events": events}
    data.update(overrides)
    return apply_zoom.parse_sidecar(data)


def _ev(t, x, y, kind=None):
    event = {"t": t, "x": x, "y": y}
    if kind:
        event["type"] = kind
    return event


class _Render:
    """Zoom и левый верхний угол кадра zoompan для номера кадра."""

    def __init__(self, settings):
        self.segments = apply_zoom.segments_from(settings)
        ramp = apply_zoom.ramp_frames(settings["ramp_sec"], settings["fps"])
        self.z = apply_zoom.zoom_expr(self.segments, settings["zoom_factor"], ramp)
        self.x = apply_zoom.pan_expr(self.segments, "x", settings["viewport"])
        self.y = apply_zoom.pan_expr(self.segments, "y", settings["viewport"])

    def zoom(self, frame):
        return _evaluate(self.z, on=frame)

    def origin(self, frame, iw=W, ih=H):
        zoom = self.zoom(frame)
        return (_evaluate(self.x, on=frame, iw=iw, zoom=zoom),
                _evaluate(self.y, on=frame, ih=ih, zoom=zoom))


# --- ramp и плавность ---

def test_ramp_frames():
    assert apply_zoom.ramp_frames(0.5, 25) == 12
    assert apply_zoom.ramp_frames(1.0, 30) == 30
    # Нулевой ramp — один кадр, а не деление на ноль.
    assert apply_zoom.ramp_frames(0, 25) == 1


def test_zoom_in_hold_out_timeline():
    render = _Render(_settings([_ev(2.0, 640, 360, "in"), _ev(6.0, 640, 360, "out")]))
    assert render.segments == [(50, 150, 640, 360)]
    assert render.zoom(0) == 1
    assert render.zoom(50) == 1                         # zoom-in начинается на кадре события
    assert 1 < render.zoom(56) < 2                      # середина разгона
    assert render.zoom(50 + RAMP) == 2                  # hold
    assert render.zoom(149) == 2
    assert render.zoom(150) == 2                        # zoom-out начинается на кадре второго нажатия
    assert 1 < render.zoom(156) < 2
    assert render.zoom(150 + RAMP) == 1
    assert render.zoom(1000) == 1


def test_ramp_is_eased_not_linear():
    render = _Render(_settings([_ev(2.0, 640, 360, "in")]))
    levels = [render.zoom(50 + i) - 1 for i in range(RAMP + 1)]
    steps = [b - a for a, b in zip(levels, levels[1:])]
    assert all(step > 0 for step in steps)
    # Ease-in/ease-out: в начале и в конце шаги мельче, чем в середине.
    assert steps[0] < steps[RAMP // 2] > steps[-1]
    assert levels[RAMP // 2] == pytest.approx(0.5)


def test_out_during_ramp_starts_from_current_level():
    # out через 0,2 с: zoom-in не успел закончиться, zoom-out начинается с достигнутого уровня без скачка.
    render = _Render(_settings([_ev(2.0, 640, 360, "in"), _ev(2.2, 640, 360, "out")]))
    levels = [render.zoom(n) for n in range(45, 80)]
    assert max(levels) < 2
    assert all(abs(b - a) < 0.2 for a, b in zip(levels, levels[1:]))
    assert levels[-1] == 1


# --- координаты и границы кадра ---

def test_centered_on_mouse_point():
    render = _Render(_settings([_ev(1.0, 640, 360, "in")]))
    # zoom 2: кадр 640x360 с центром в (640, 360).
    assert render.origin(25 + RAMP) == (320, 180)


def test_crop_clamped_to_frame():
    render = _Render(_settings([_ev(1.0, 1270, 5, "in")]))
    # Точка у правого верхнего угла: кадр прижат к границе, а не выходит за неё.
    assert render.origin(25 + RAMP) == (W - W / 2, 0)

    render = _Render(_settings([_ev(1.0, 3, 715, "in")]))
    assert render.origin(25 + RAMP) == (0, H - H / 2)


def test_crop_stays_in_frame_during_ramps():
    render = _Render(_settings([_ev(1.0, 1279, 0, "in"), _ev(3.0, 1279, 0, "out")]))
    for frame in range(0, 120):
        zoom = render.zoom(frame)
        x, y = render.origin(frame)
        assert 0 <= x <= W - W / zoom + 1e-9
        assert 0 <= y <= H - H / zoom + 1e-9


def test_no_zoom_means_full_frame():
    render = _Render(_settings([_ev(2.0, 1000, 600, "in"), _ev(4.0, 1000, 600, "out")]))
    assert render.origin(0) == (0, 0)
    assert render.origin(500) == (0, 0)


def test_mouse_coords_scaled_to_video_pixels():
    # Viewport 1280x720 → видео 1280x720: 1:1. Если бы видео было 2560x1440, точка масштабировалась бы.
    render = _Render(_settings([_ev(1.0, 640, 360, "in")]))
    assert render.origin(25 + RAMP, iw=2 * W, ih=2 * H) == (640, 360)


# --- несколько пар и открытый zoom ---

def test_multiple_pairs_use_their_own_points():
    render = _Render(_settings([
        _ev(1.0, 200, 150, "in"), _ev(3.0, 200, 150, "out"),
        _ev(6.0, 900, 500, "in"), _ev(8.0, 900, 500, "out"),
    ]))
    assert render.segments == [(25, 75, 200, 150), (150, 200, 900, 500)]
    assert render.zoom(60) == 2
    assert render.origin(60) == (0, 0)                  # (200,150) у угла: прижато
    assert render.zoom(110) == 1                        # между парами zoom снят
    assert render.zoom(180) == 2
    assert render.origin(180) == (900 - 320, 500 - 180)
    assert render.zoom(300) == 1


def test_next_zoom_in_waits_for_previous_zoom_out():
    # Второй in раньше, чем закончился zoom-out первой пары: начинается после него.
    render = _Render(_settings([
        _ev(1.0, 200, 150, "in"), _ev(3.0, 200, 150, "out"),
        _ev(3.1, 1000, 500, "in"), _ev(5.0, 1000, 500, "out"),
    ]))
    assert render.segments == [(25, 75, 200, 150), (75 + RAMP, 125, 1000, 500)]
    assert render.zoom(75 + RAMP) == 1                  # точка меняется только при zoom 1
    assert max(render.zoom(n) for n in range(0, 200)) <= 2


def test_open_ended_zoom_holds_to_end():
    render = _Render(_settings([_ev(1.0, 200, 150, "in"), _ev(3.0, 200, 150, "out"), _ev(5.0, 700, 400, "in")]))
    assert render.segments == [(25, 75, 200, 150), (125, None, 700, 400)]
    assert render.zoom(125 + RAMP) == 2
    assert render.zoom(10 ** 6) == 2
    assert render.origin(10 ** 6) == (700 - 320, 400 - 180)


def test_pairing_ignores_stray_events():
    events = [_ev(0.5, 1, 1, "out"), _ev(1, 10, 10, "in"), _ev(2, 20, 20, "in"), _ev(3, 30, 30, "out"),
              _ev(4, 40, 40, "out")]
    pairs = apply_zoom.zoom_pairs(_settings(events)["events"])
    assert [(a["t"], b["t"]) for a, b in pairs] == [(1, 3)]


# --- sidecar ---

def test_sidecar_path_next_to_video():
    assert apply_zoom.sidecar_path("video/Muster GmbH.webm") == "video/Muster GmbH.zoom.json"


def test_load_sidecar(tmp_path):
    path = tmp_path / "a.zoom.json"
    path.write_text(json.dumps({
        "viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5,
        "events": [{"t": 8.0, "x": 640, "y": 360, "type": "out"}, {"t": 3.0, "x": 640, "y": 360, "type": "in"}],
    }), encoding="utf-8")
    settings = apply_zoom.load_sidecar(str(path))
    assert settings["viewport"] == {"width": 1280, "height": 720}
    assert settings["fps"] == 25 and settings["zoom_factor"] == 2 and settings["ramp_sec"] == 0.5
    # События сортируются по времени.
    assert [e["type"] for e in settings["events"]] == ["in", "out"]


def test_old_sidecar_without_type_alternates():
    settings = apply_zoom.parse_sidecar({"events": [
        {"t": 1, "x": 1, "y": 1}, {"t": 2, "x": 2, "y": 2}, {"t": 3, "x": 3, "y": 3},
    ]})
    assert [e["type"] for e in settings["events"]] == ["in", "out", "in"]
    # Остальные поля — по умолчанию.
    assert settings["viewport"] == apply_zoom.DEFAULT_VIEWPORT
    assert settings["fps"] == apply_zoom.DEFAULT_FPS
    assert len(apply_zoom.segments_from(settings)) == 2


@pytest.mark.parametrize("data", [
    [],
    {"events": [{"t": 1, "x": 1}]},
    {"events": [{"t": 1, "x": 1, "y": 1, "type": "zoom"}]},
    {"fps": 0, "events": []},
    {"zoom_factor": 0.5, "events": []},
    {"viewport": {"width": 0, "height": 720}, "events": []},
])
def test_invalid_sidecar(data):
    with pytest.raises(ValueError):
        apply_zoom.parse_sidecar(data)


# --- ffmpeg-команда и CLI ---

def test_build_command():
    settings = _settings([_ev(2.0, 640, 360, "in"), _ev(6.0, 640, 360, "out")])
    command = apply_zoom.build_command("/opt/ffmpeg", "in.webm", "out.webm", settings,
                                       apply_zoom.segments_from(settings))
    assert command[0] == "/opt/ffmpeg"
    assert command[command.index("-i") + 1] == "in.webm"
    assert command[-1] == "out.webm"
    assert command[command.index("-c:v") + 1] == "libvpx-vp9"
    vf = command[command.index("-vf") + 1]
    assert vf.startswith("fps=25,zoompan=")
    assert ":d=1:fps=25:s=1280x720" in vf


def test_no_events_copies_unchanged(tmp_path):
    video = tmp_path / "v.webm"
    video.write_bytes(b"original video")
    out = tmp_path / "v_zoomed.webm"
    assert apply_zoom.render(str(video), str(out), _settings([])) is None
    assert out.read_bytes() == b"original video"
    # Без sidecar — тоже копия.
    out.unlink()
    assert apply_zoom.render(str(video), str(out), None) is None
    assert out.read_bytes() == b"original video"


def test_cli_dry_run_prints_command(tmp_path, capsys, monkeypatch):
    video = tmp_path / "ACME_GmbH.webm"
    video.write_bytes(b"video")
    (tmp_path / "ACME_GmbH.zoom.json").write_text(json.dumps({
        "events": [{"t": 3.0, "x": 640, "y": 360, "type": "in"}, {"t": 8.0, "x": 640, "y": 360, "type": "out"}],
    }), encoding="utf-8")

    def no_ffmpeg(*args, **kwargs):
        raise AssertionError("--dry-run must not run ffmpeg")

    monkeypatch.setattr(apply_zoom.subprocess, "run", no_ffmpeg)
    assert apply_zoom.main(["--video", str(video), "--dry-run", "--ffmpeg", "my-ffmpeg"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("my-ffmpeg ")
    assert "zoompan" in printed
    assert "ACME_GmbH_zoomed.webm" in printed
    assert not (tmp_path / "ACME_GmbH_zoomed.webm").exists()


def test_cli_without_sidecar_copies(tmp_path, capsys):
    video = tmp_path / "plain.webm"
    video.write_bytes(b"video")
    assert apply_zoom.main(["--video", str(video)]) == 0
    assert (tmp_path / "plain_zoomed.webm").read_bytes() == b"video"


def test_missing_ffmpeg_reported(tmp_path):
    settings = _settings([_ev(1.0, 640, 360, "in")])
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        apply_zoom.render(str(tmp_path / "in.webm"), str(tmp_path / "out.webm"), settings,
                          ffmpeg=str(tmp_path / "no-such-ffmpeg"))
