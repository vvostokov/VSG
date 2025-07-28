from app import create_app # Импортируем фабрику приложений
from extensions import db # db теперь в extensions
from models import * # Импортируем все модели, чтобы метаданные были зарегистрированы

# Для операций с базой данных вне контекста запроса Flask,
# нам нужно явно создать контекст приложения.
app = create_app()
with app.app_context():
    # Удаляем все таблицы, определенные в db.metadata, используя движок из db
    db.metadata.drop_all(bind=db.engine)
    print("Все таблицы удалены.")
    # Создаем все таблицы заново
    db.metadata.create_all(bind=db.engine)
    print("Все таблицы созданы заново.")