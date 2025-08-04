import click
from flask.cli import AppGroup
from analytics_logic import (
    refresh_crypto_price_change_data,
    refresh_crypto_portfolio_history,
    refresh_securities_portfolio_history,
    refresh_performance_chart_data
)
from models import Bank, Category
from extensions import db
from data_seeds import DEFAULT_BANKS, DEFAULT_CATEGORIES

# Создаем группу команд 'analytics' для удобства
analytics_cli = AppGroup('analytics', help='Команды для аналитики и обновления данных.')

# Новая группа команд для заполнения базы данных
seed_cli = AppGroup('seed', help='Команды для заполнения базы данных начальными данными.')

@seed_cli.command('banks')
def seed_banks_command():
    """Populates the database with a list of common banks."""
    existing_banks = {b.name for b in Bank.query.all()}
    added_count = 0
    for bank_name in DEFAULT_BANKS:
        if bank_name not in existing_banks:
            db.session.add(Bank(name=bank_name))
            added_count += 1
    db.session.commit()
    print(f"Добавлено {added_count} новых банков.")

@seed_cli.command('categories')
def seed_categories_command():
    """Populates the database with default categories and subcategories."""
    added_count = 0
    for cat_type, main_categories in DEFAULT_CATEGORIES.items():
        for main_cat_name, sub_cat_names in main_categories.items():
            main_cat = Category.query.filter_by(name=main_cat_name, type=cat_type, parent_id=None).first()
            if not main_cat:
                main_cat = Category(name=main_cat_name, type=cat_type, parent_id=None)
                db.session.add(main_cat)
                db.session.commit()
                added_count += 1
            for sub_cat_name in sub_cat_names:
                sub_cat = Category.query.filter_by(name=sub_cat_name, type=cat_type, parent_id=main_cat.id).first()
                if not sub_cat:
                    db.session.add(Category(name=sub_cat_name, type=cat_type, parent_id=main_cat.id))
                    added_count += 1
    db.session.commit()
    print(f"Добавлено {added_count} новых категорий и подкатегорий.")

@analytics_cli.command('refresh-crypto-prices')
def refresh_crypto_prices_command():
    """Обновляет кэш с изменениями цен для криптоактивов."""
    print("Запуск обновления кэша цен криптоактивов...")
    success, message = refresh_crypto_price_change_data()
    print(message)

@analytics_cli.command('refresh-performance-chart')
def refresh_performance_chart_command():
    """Обновляет данные для графика производительности."""
    print("Запуск обновления данных для графика производительности...")
    success, message = refresh_performance_chart_data()
    print(message)

@analytics_cli.command('refresh-all-history')
def refresh_all_history_command():
    """Пересчитывает историю стоимости для всех портфелей."""
    print("--- НАЧАЛО ПЕРЕСЧЕТА ИСТОРИИ ПОРТФЕЛЕЙ ---")
    print("\n-> Пересчет истории крипто-портфеля...")
    success, message = refresh_crypto_portfolio_history()
    print(message)
    print("\n-> Пересчет истории портфеля ЦБ...")
    success, message = refresh_securities_portfolio_history()
    print(message)
    print("\n--- ПЕРЕСЧЕТ ИСТОРИИ ПОРТФЕЛЕЙ ЗАВЕРШЕН ---")

@analytics_cli.command('refresh-all')
def refresh_all_command():
    """Запускает все основные задачи по обновлению аналитических данных."""
    print("--- НАЧАЛО ПОЛНОГО ОБНОВЛЕНИЯ АНАЛИТИКИ ---")
    for name, func in [
        ("кэша цен криптоактивов", refresh_crypto_price_change_data),
        ("графика производительности", refresh_performance_chart_data),
        ("истории крипто-портфеля", refresh_crypto_portfolio_history),
        ("истории портфеля ЦБ", refresh_securities_portfolio_history)
    ]:
        print(f"\n-> Обновление {name}...")
        success, message = func()
        print(message)
    print("\n--- ПОЛНОЕ ОБНОВЛЕНИЕ АНАЛИТИКИ ЗАВЕРШЕНО ---")