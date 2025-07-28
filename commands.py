import click
from flask.cli import AppGroup
from analytics_logic import (
    refresh_crypto_price_change_data,
    refresh_crypto_portfolio_history,
    refresh_securities_portfolio_history,
    refresh_performance_chart_data
)

# Создаем группу команд 'analytics' для удобства
analytics_cli = AppGroup('analytics', help='Команды для аналитики и обновления данных.')

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