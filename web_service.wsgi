"""Точка входа mod_wsgi: Apache загружает этот файл и берёт из него application."""
from web_service import app as application, init_storage

# Скрипт загружается при старте процесса (см. apache.conf), поэтому сервис сначала дожидается базы.
init_storage()
