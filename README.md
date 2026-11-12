# Веб-сервис видео в Docker

Два контейнера под управлением Docker Compose:

- `web` — Apache + mod_wsgi, внутри Flask-приложение `web_service.py` (Python 3.9);
- `db` — PostgreSQL 16.

Apache и PostgreSQL работают только внутри контейнеров: на саму машину их ставить не нужно, достаточно Docker.

| Файл | Назначение |
|---|---|
| `Dockerfile` | образ `web`: Apache, mod_wsgi, собранный под Python образа, зависимости |
| `docker-compose.yml` | сервисы `web` и `db`, тома `db_data` и `uploads`, healthcheck базы |
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
