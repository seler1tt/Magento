"""Рендерит zoom в стиле Camtasia по событиям из <видео>.zoom.json через фильтр ffmpeg zoompan.

    python apply_zoom.py --video video/ACME_GmbH.webm
    python apply_zoom.py --video video/ACME_GmbH.webm --dry-run
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys

SIDECAR_SUFFIX = ".zoom.json"
OUTPUT_SUFFIX = "_zoomed"
ZOOMED_EXTENSION = ".webm"

# Значения для sidecar-ов, где поля нет; совпадают с настройками рекордера.
DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
DEFAULT_FPS = 25
DEFAULT_ZOOM_FACTOR = 2.0
DEFAULT_RAMP_SEC = 0.5
# zoompan не увеличивает больше чем в 10 раз.
MAX_ZOOM_FACTOR = 10

VP9_CRF = 32
STDERR_TAIL_CHARS = 2000


def sidecar_path(video_path):
    return os.path.splitext(video_path)[0] + SIDECAR_SUFFIX


def parse_sidecar(data):
    """Проверяет содержимое sidecar и подставляет значения по умолчанию; события сортируются по времени."""
    try:
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        viewport = data.get("viewport") or DEFAULT_VIEWPORT
        settings = {
            "viewport": {"width": int(viewport["width"]), "height": int(viewport["height"])},
            "fps": float(data.get("fps", DEFAULT_FPS)),
            "zoom_factor": float(data.get("zoom_factor", DEFAULT_ZOOM_FACTOR)),
            "ramp_sec": float(data.get("ramp_sec", DEFAULT_RAMP_SEC)),
            "events": [],
        }
        if settings["viewport"]["width"] <= 0 or settings["viewport"]["height"] <= 0:
            raise ValueError("viewport must be positive")
        if settings["fps"] <= 0:
            raise ValueError("fps must be positive")
        if not 1 <= settings["zoom_factor"] <= MAX_ZOOM_FACTOR:
            raise ValueError(f"zoom_factor must be between 1 and {MAX_ZOOM_FACTOR}")
        if settings["ramp_sec"] < 0:
            raise ValueError("ramp_sec must be >= 0")

        previous = "out"
        for raw in sorted(data.get("events") or [], key=lambda e: float(e["t"])):
            # В старых sidecar-ах нет type: события там чередуются, как нажатия F5.
            kind = raw.get("type") or ("in" if previous == "out" else "out")
            if kind not in ("in", "out"):
                raise ValueError(f"unknown event type {kind!r}")
            settings["events"].append(
                {"t": float(raw["t"]), "x": float(raw["x"]), "y": float(raw["y"]), "type": kind}
            )
            previous = kind
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid zoom sidecar: {exc}") from exc
    return settings


def load_sidecar(path):
    with open(path, encoding="utf-8") as f:
        return parse_sidecar(json.load(f))


def zoom_pairs(events):
    """Каждый in в паре со следующим out. in без out даёт (in, None): zoom держится до конца видео.
    Лишние события (out без in, повторный in) пропускаются."""
    pairs = []
    zoom_in = None
    for event in events:
        if event["type"] == "in":
            if zoom_in is None:
                zoom_in = event
        elif zoom_in is not None:
            pairs.append((zoom_in, event))
            zoom_in = None
    if zoom_in is not None:
        pairs.append((zoom_in, None))
    return pairs


def ramp_frames(ramp_sec, fps):
    # Хотя бы один кадр: с нулевой длиной формула zoom делила бы на ноль.
    return max(int(round(ramp_sec * fps)), 1)


def zoom_segments(pairs, fps, ramp):
    """Переводит пары в кадры: (кадр начала zoom-in, кадр начала zoom-out или None, x, y).

    Следующий zoom-in начинается не раньше, чем закончится zoom-out предыдущего: иначе центр
    перескочил бы на новую точку, пока кадр ещё увеличен."""
    segments = []
    free_from = 0
    for zoom_in, zoom_out in pairs:
        start = max(int(round(zoom_in["t"] * fps)), free_from)
        if zoom_out is None:
            segments.append((start, None, zoom_in["x"], zoom_in["y"]))
            break
        out = max(int(round(zoom_out["t"] * fps)), start)
        segments.append((start, out, zoom_in["x"], zoom_in["y"]))
        free_from = out + ramp
    return segments


def segments_from(settings):
    """Сегменты zoom из sidecar; settings=None (sidecar нет) — сегментов нет."""
    if settings is None:
        return []
    ramp = ramp_frames(settings["ramp_sec"], settings["fps"])
    return zoom_segments(zoom_pairs(settings["events"]), settings["fps"], ramp)


# Выражения ниже записаны общим подмножеством синтаксиса ffmpeg и Python (числа, + - * /, вызовы
# функций), поэтому тесты вычисляют их без ffmpeg. on — номер выходного кадра zoompan.

def _ease(p):
    # smoothstep: плавный разгон и торможение вместо «роботизированного» линейного ramp.
    return f"{p}*{p}*(3-2*{p})"


def zoom_expr(segments, zoom_factor, ramp):
    terms = []
    for start, out, _, _ in segments:
        term = _ease(f"clip((on-{start})/{ramp},0,1)")
        if out is not None:
            # min: если zoom-out начался раньше, чем закончился zoom-in, он стартует с текущего уровня.
            term = f"min({term},1-{_ease(f'clip((on-{out})/{ramp},0,1)')})"
        terms.append(term)
    return f"1+{zoom_factor - 1:g}*({'+'.join(terms)})"


def _center_expr(segments, index, size_var, viewport_size):
    # Точка zoom меняется скачком в начале каждого сегмента: в этот момент кадр ещё не увеличен.
    parts = [f"{segments[0][index]:g}"]
    for prev, seg in zip(segments, segments[1:]):
        parts.append(f"{seg[index] - prev[index]:+g}*gte(on,{seg[0]})")
    # Координаты мыши в пикселях viewport; при record_video_size == viewport масштаб равен 1.
    return f"({''.join(parts)})*{size_var}/{viewport_size}"


def pan_expr(segments, axis, viewport):
    """Левый (верхний) край кадра zoompan: центр на точке мыши, но кадр не выходит за границы видео."""
    if axis == "x":
        size_var, viewport_size, index = "iw", viewport["width"], 2
    else:
        size_var, viewport_size, index = "ih", viewport["height"], 3
    center = _center_expr(segments, index, size_var, viewport_size)
    return f"clip({center}-{size_var}/(2*zoom),0,{size_var}-{size_var}/zoom)"


def build_filter(settings, segments):
    fps = f"{settings['fps']:g}"
    ramp = ramp_frames(settings["ramp_sec"], settings["fps"])
    viewport = settings["viewport"]
    z = zoom_expr(segments, settings["zoom_factor"], ramp)
    x = pan_expr(segments, "x", viewport)
    y = pan_expr(segments, "y", viewport)
    # fps перед zoompan: номер кадра on однозначно соответствует времени on/fps.
    # d=1 — один выходной кадр на каждый входной, длительность видео не меняется.
    # Видео рекордера имеет размер viewport, поэтому s=viewport сохраняет разрешение.
    return (
        f"fps={fps},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:fps={fps}:s={viewport['width']}x{viewport['height']}"
    )


def build_command(ffmpeg, video_path, output_path, settings, segments):
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path,
        "-vf", build_filter(settings, segments),
        "-c:v", "libvpx-vp9", "-crf", str(VP9_CRF), "-b:v", "0", "-row-mt", "1",
        "-an",
        # Формат задан явно: вызывающий код может писать во временный файл с другим расширением.
        "-f", "webm",
        output_path,
    ]


def output_extension(video_path, segments):
    """С zoom получается .webm; без событий видео копируется и сохраняет своё расширение."""
    return ZOOMED_EXTENSION if segments else os.path.splitext(video_path)[1]


def render(video_path, output_path, settings, ffmpeg="ffmpeg", dry_run=False):
    """Рендерит zoom в output_path; без zoom-событий копирует видео без изменений.

    Возвращает команду ffmpeg или None, если видео копируется."""
    segments = segments_from(settings)
    if not segments:
        if not dry_run:
            shutil.copyfile(video_path, output_path)
        return None
    command = build_command(ffmpeg, video_path, output_path, settings, segments)
    if dry_run:
        return command
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        raise RuntimeError(f"ffmpeg not found: {ffmpeg!r} (use --ffmpeg <path>)") from None
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg exited with code {result.returncode}: {result.stderr[-STDERR_TAIL_CHARS:].strip()}"
        )
    return command


def format_command(command):
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", required=True, help="исходное видео; sidecar ищется рядом: <имя>.zoom.json")
    parser.add_argument("--dry-run", action="store_true", help="только напечатать команду ffmpeg")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="путь к ffmpeg, если его нет в PATH")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    if not os.path.isfile(args.video):
        print(f"Video not found: {args.video}", file=sys.stderr)
        return 1

    sidecar = sidecar_path(args.video)
    try:
        # Рекордер пишет sidecar, только если были события: нет файла — нет zoom.
        settings = load_sidecar(sidecar) if os.path.exists(sidecar) else None
    except (OSError, ValueError) as exc:
        print(f"Cannot read {sidecar}: {exc}", file=sys.stderr)
        return 1

    segments = segments_from(settings)
    stem = os.path.splitext(args.video)[0]
    output = f"{stem}{OUTPUT_SUFFIX}{output_extension(args.video, segments)}"
    try:
        command = render(args.video, output, settings, ffmpeg=args.ffmpeg, dry_run=args.dry_run)
    except (OSError, RuntimeError) as exc:
        print(f"Zoom failed for {args.video}: {exc}", file=sys.stderr)
        return 1

    if command is None:
        action = "Would copy" if args.dry_run else "Copied"
        print(f"No zoom events in {sidecar}. {action} unchanged: {output}")
    elif args.dry_run:
        print(format_command(command))
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
