# Веб-сервис видео в Docker

Три контейнера под управлением Docker Compose:

- `web` — Apache + mod_wsgi, внутри Flask-приложение `web_service.py` (Python 3.9);
- `worker` — `worker.py`: zoom-обработка загруженных видео через ffmpeg (тот же образ, что и `web`);
- `db` — PostgreSQL 16.

Apache и PostgreSQL работают только внутри контейнеров: на саму машину их ставить не нужно, достаточно Docker.

| Файл | Назначение |
|---|---|
| `Dockerfile` | образ `web` и `worker`: Apache, mod_wsgi, собранный под Python образа, ffmpeg, зависимости |
| `docker-compose.yml` | сервисы `web`, `worker` и `db`, тома `db_data` и `uploads`, healthcheck базы |
| `worker.py` | фоновая zoom-обработка: берёт видео со статусом `pending`, пишет результат в PostgreSQL |
| `apply_zoom.py` | рендер zoom по sidecar `<видео>.zoom.json` через ffmpeg `zoompan` |
| `web_service.wsgi` | точка входа mod_wsgi |
| `apache.conf` | VirtualHost: WSGI-приложение и раздача `/media/` из `uploads/` через Alias |
| `.env.example` | образец настроек подключения к базе |

## Подготовка (Windows)

1. Установите [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) с бэкендом WSL 2
   (установщик сам предложит включить WSL 2; после установки может потребоваться перезагрузка).
2. Запустите Docker Desktop и проверьте в PowerShell:

   ```powershell
   docker compose version
   ```

## Запуск

Из папки проекта:

```powershell
Copy-Item .env.example .env   # в Linux/macOS: cp .env.example .env
# при желании поменяйте DB_PASSWORD в .env
docker compose up --build
```

Сервис доступен на `http://127.0.0.1:8000`. Контейнер `web` стартует только после того, как `db` пройдёт
healthcheck, а при старте приложение ещё и само дожидается подключения к базе.

Запуск в фоне: `docker compose up --build -d`; логи: `docker compose logs -f web`; остановка: `docker compose down`.

## Проверка

```powershell
# UploadVideo
curl.exe -F "company_name=Muster GmbH" -F "file=@video/Muster GmbH.webm" http://127.0.0.1:8000/upload
# GetVideoURL
curl.exe "http://127.0.0.1:8000/get_video_url?company_name=Muster%20GmbH"
# список записей
curl.exe http://127.0.0.1:8000/videos
# изнутри контейнера
docker compose exec web curl http://localhost/videos
```

Возвращённый `video_url` (`http://127.0.0.1:8000/media/Muster_GmbH.webm`) открывается и воспроизводится в браузере.

## Zoom-обработка в сервисе

`/upload` принимает необязательное поле `zoom` — sidecar с zoom-событиями (`UploadVideo` из `video_client.py`
отправляет его сам, если рядом с видео лежит `<имя>.zoom.json`):

```powershell
curl.exe -F "company_name=Muster GmbH" -F "file=@video/Muster GmbH.webm" -F "zoom=@video/Muster GmbH.zoom.json" http://127.0.0.1:8000/upload
```

Sidecar сохраняется рядом с видео в томе `uploads`. Каждая загрузка получает статус `pending`; контейнер `worker`
забирает такие записи, запускает `apply_zoom.py` и сохраняет в таблице `videos`:

| Колонка | Значение |
|---|---|
| `processing_status` | `pending` → `processing` → `done`, `skipped` (загружено без sidecar) или `failed` |
| `processed_video_url` | URL обработанного видео: `http://127.0.0.1:8000/media/processed/Muster_GmbH.webm` |
| `processing_error` | текст ошибки при `failed` |

Статус и URL результата видны в `curl.exe http://127.0.0.1:8000/videos`, лог обработки — `docker compose logs -f worker`.
Повторная загрузка видео снова ставит его в очередь. Колонки добавляются к уже существующей таблице при старте сервиса.

## Сохранность данных

База лежит в именованном томе `db_data`, загруженные видео — в томе `uploads`. После
`docker compose down` и повторного `docker compose up` записи и файлы остаются на месте.
Удалить всё вместе с данными: `docker compose down -v`.

## Рекордер

Рекордер запускается на хосте (Python 3.9+):

```powershell
pip install -r requirements.txt
python -m playwright install chromium
python capture_video.py --server http://127.0.0.1:8000
```

`mod-wsgi` в `requirements.txt` помечен `sys_platform == "linux"`: он собирается только в контейнере,
а на Windows pip его пропускает.

### Zoom во время записи

- **F5** в окне браузера переключает zoom: первое нажатие — начало zoom-in в точке, где сейчас мышь, второе —
  начало zoom-out. Страница при этом не перезагружается. Цветное кольцо показывает переключение:
  зелёное — zoom-in, синее — zoom-out.
- **`window.__zoomAt(x, y)`** делает то же переключение из сценария. Сценарий `qscript.py` сам показывает zoom
  на первом результате поиска перед кликом; `python capture_video.py --no-auto-zoom` отключает это, и тогда
  zoom-события записываются только по F5.
- Если было хотя бы одно событие, рядом с видео пишется sidecar `video/<имя>.zoom.json`:

  ```json
  {
    "viewport": {"width": 1280, "height": 720},
    "fps": 25,
    "zoom_factor": 2.0,
    "ramp_sec": 0.5,
    "events": [
      {"t": 3.0, "x": 640, "y": 360, "type": "in"},
      {"t": 8.0, "x": 640, "y": 360, "type": "out"}
    ]
  }
  ```

  `t` — секунды от начала видео, `x`/`y` — пиксели viewport (видео записывается в размере viewport, поэтому они
  совпадают с пикселями видео).

### Zoom локально: apply_zoom.py

```powershell
python apply_zoom.py --video "video/Muster GmbH.webm"            # -> video/Muster GmbH_zoomed.webm
python apply_zoom.py --video "video/Muster GmbH.webm" --dry-run  # только напечатать команду ffmpeg
python apply_zoom.py --video "video/Muster GmbH.webm" --ffmpeg C:\ffmpeg\bin\ffmpeg.exe
```

Каждый `in` в паре со следующим `out`: плавный zoom-in (`ramp_sec`), удержание до `out`, плавный zoom-out.
`in` без `out` держит zoom до конца видео. Кадр центрируется на точке мыши и не выходит за границы видео.
Без sidecar или без событий видео копируется без изменений. Результат — VP9 `.webm` в исходном разрешении.

## Тесты

```powershell
pip install -r requirements.txt
python -m pytest
```

Интеграционные тесты (`tests/test_docker_integration.py`: ffmpeg в образах `web` и `worker`, zoom-обработка
загруженного видео) запускаются при поднятом `docker compose up` и пропускаются без адреса сервиса:

```powershell
$env:ZOOM_SERVICE_URL = "http://127.0.0.1:8000"; python -m pytest tests/test_docker_integration.py
```
