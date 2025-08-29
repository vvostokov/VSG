#!/bin/sh

set -e

# Ожидаем, пока база данных станет доступной
echo "Waiting for database to be ready..."
python wait-for-db.py

# Этот скрипт выполняется при запуске контейнера

# Применяем миграции базы данных
echo "Running database migrations..."
flask db upgrade

# Запускаем Gunicorn сервер
echo "Starting Gunicorn..."
gunicorn --bind :8080 --workers 3 --timeout 180 "app:create_app()"