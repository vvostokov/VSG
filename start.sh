#!/bin/sh

# Этот скрипт выполняется при запуске контейнера

# Применяем миграции базы данных
echo "Running database migrations..."
flask db upgrade

# Запускаем Gunicorn сервер
echo "Starting Gunicorn..."
gunicorn --bind :8080 --workers 3 "app:create_app()"