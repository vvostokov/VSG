import os
import socket
import time
from urllib.parse import urlparse

# Получаем DATABASE_URL из переменных окружения
db_url = os.environ.get("DATABASE_URL")

if not db_url:
    print("Переменная окружения DATABASE_URL не установлена. Пропускаем ожидание.")
    exit(0)

# Парсим URL, чтобы извлечь хост и порт
try:
    parsed_url = urlparse(db_url)
    db_host = parsed_url.hostname
    db_port = parsed_url.port or 5432 # Порт по умолчанию для PostgreSQL
except Exception as e:
    print(f"Ошибка парсинга DATABASE_URL: {e}")
    exit(1)

if not db_host:
    print("Не удалось извлечь хост из DATABASE_URL. Пропускаем ожидание.")
    exit(0)

print(f"Ожидание доступности базы данных по адресу: {db_host}:{db_port}...")

for i in range(12): # Пытаемся в течение 60 секунд (12 * 5 сек)
    try:
        with socket.create_connection((db_host, db_port), timeout=5):
            print("База данных доступна! Продолжаем запуск...")
            exit(0)
    except (socket.timeout, ConnectionRefusedError, socket.gaierror) as e:
        print(f"База данных еще не готова ({e}). Повторная попытка через 5 секунд...")
        time.sleep(5)

print(f"Не удалось подключиться к базе данных в течение 60 секунд. Запуск прерван.")
exit(1)