FROM python:3.9-slim-bookworm

# apache2-dev и gcc нужны, чтобы pip собрал mod_wsgi под Python этого образа, libpq-dev — для psycopg2.
# curl — для проверки изнутри контейнера: docker compose exec web curl http://localhost/
RUN apt-get update \
    && apt-get install -y --no-install-recommends apache2 apache2-dev gcc libc6-dev libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# module-config печатает LoadModule и WSGIPythonHome для только что собранного модуля.
RUN mod_wsgi-express module-config > /etc/apache2/mods-available/wsgi.load \
    && a2enmod wsgi

COPY apache.conf /etc/apache2/sites-available/video.conf
RUN a2dissite 000-default && a2ensite video \
    && ln -sf /dev/stdout /var/log/apache2/access.log \
    && ln -sf /dev/stderr /var/log/apache2/error.log

COPY web_service.py web_service.wsgi ./
# Новый именованный том uploads получает владельца этой папки, и Apache (www-data) может в него писать.
RUN mkdir -p uploads && chown www-data:www-data uploads

EXPOSE 80

# exec: Apache становится PID 1 и сразу завершается по docker compose down.
CMD ["/bin/sh", "-c", ". /etc/apache2/envvars && mkdir -p \"$APACHE_RUN_DIR\" \"$APACHE_LOCK_DIR\" && exec apache2 -D FOREGROUND"]
