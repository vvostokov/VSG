import os
from app import create_app # Импортируем фабрику приложений

# Для операций с базой данных вне контекста запроса Flask,
# нам нужно явно создать контекст приложения.
app = create_app()
with app.app_context():
    # ПРАВИЛЬНЫЙ СПОСОБ: Полное удаление файла базы данных.
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI')
    if db_uri and db_uri.startswith('sqlite:///'):
        # Извлекаем путь к файлу из URI.
        # os.path.abspath гарантирует, что мы работаем с полным путем.
        db_path = os.path.abspath(db_uri.split('sqlite:///', 1)[1])
        if os.path.exists(db_path):
            os.remove(db_path)
            print(f"Файл базы данных '{db_path}' был полностью удален.")
        else:
            print(f"Файл базы данных '{db_path}' не найден, удаление не требуется.")
    else:
        print("Скрипт настроен на удаление только файла SQLite.")

    print("\nБаза данных полностью очищена. Теперь выполните 'flask db upgrade' для создания схемы с нуля.")