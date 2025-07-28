# Используем официальный образ Python в качестве базового
FROM python:3.11-slim

# Устанавливаем переменные окружения для Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Устанавливаем системные зависимости, которые могут понадобиться для psycopg2 (PostgreSQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код приложения
COPY . .

# Делаем скрипт запуска исполняемым
RUN chmod +x ./start.sh

# Команда, которая будет выполнена при запуске контейнера
CMD ["./start.sh"]