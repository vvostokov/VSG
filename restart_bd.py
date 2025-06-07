from app import app, db # Импортируем app для контекста и db для операций с БД

# Для операций с базой данных вне контекста запроса Flask,
# нам нужно явно создать контекст приложения.
with app.app_context():
    # Удаляем все таблицы, определенные в db.metadata, используя движок из db
    db.metadata.drop_all(bind=db.engine)
    print("Все таблицы удалены.")
    # Создаем все таблицы заново
    db.metadata.create_all(bind=db.engine)
    print("Все таблицы созданы заново.")