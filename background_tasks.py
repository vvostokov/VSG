from flask import current_app
import time
import json

from logic.news_analysis import get_news_trends_for_portfolio
from news_logic import get_crypto_news, get_securities_news
from logic.platform_sync_logic import sync_platform_balances, sync_platform_transactions
from models import InvestmentPlatform, JsonCache
from api_clients import fetch_usdt_rub_rate
from extensions import db


def update_all_news_in_background():
    """
    Фоновая задача для обновления и кэширования всех новостей.
    Эта функция будет запускаться планировщиком периодически.
    """
    # flask-apscheduler автоматически предоставляет контекст приложения для фоновых задач.
    # Явное создание приложения через create_app() здесь не требуется и вызывает ошибку.
    current_app.logger.info("--- [BG_TASK] Запуск фонового обновления новостей ---")
    try:
        # 1. Определяем топ-10 тикеров, как это делает страница новостей.
        # Нам не нужны сами тренды, только список тикеров для обновления кэша.
        _, top_10_tickers = get_news_trends_for_portfolio()

        # 2. Обновляем кэш для каждого тикера из топа.
        if top_10_tickers:
            for ticker in top_10_tickers:
                current_app.logger.info(f"--- [BG_TASK] Обновление кэша новостей для: {ticker} ---")
                get_crypto_news(categories=ticker, limit=30)

        # 3. Обновляем кэш для общих крипто-новостей (используется на главной и странице новостей).
        current_app.logger.info("--- [BG_TASK] Обновление кэша общих крипто-новостей ---")
        get_crypto_news(limit=30)  # Для /crypto-news
        get_crypto_news(limit=5)   # Для /crypto-assets (главная)

        # 4. Обновляем кэш для новостей фондового рынка.
        current_app.logger.info("--- [BG_TASK] Обновление кэша новостей фондового рынка ---")
        get_securities_news(limit=50)

        current_app.logger.info("--- [BG_TASK] Фоновое обновление новостей завершено успешно. ---")

    except Exception as e:
        current_app.logger.error(f"--- [BG_TASK] Ошибка во время фонового обновления новостей: {e}", exc_info=True)


def sync_all_platforms_in_background():
    """
    Фоновая задача для обновления балансов и транзакций по всем активным крипто-платформам.
    """
    current_app.logger.info("--- [BG_TASK] Запуск фонового обновления платформ ---")
    try:
        active_platforms = InvestmentPlatform.query.filter_by(platform_type='crypto_exchange', is_active=True).all()

        if not active_platforms:
            current_app.logger.info("--- [BG_TASK] Нет активных крипто-платформ для синхронизации.")
            return

        for platform in active_platforms:
            current_app.logger.info(f"--- [BG_TASK] Синхронизация балансов для: {platform.name} ---")
            sync_platform_balances(platform)
            time.sleep(5)  # Пауза между разными типами синхронизации

            current_app.logger.info(f"--- [BG_TASK] Синхронизация транзакций для: {platform.name} ---")
            sync_platform_transactions(platform)
            time.sleep(10)  # Пауза между платформами, чтобы не превышать лимиты API

        current_app.logger.info("--- [BG_TASK] Фоновое обновление платформ завершено успешно. ---")
    except Exception as e:
        current_app.logger.error(f"--- [BG_TASK] Ошибка во время фонового обновления платформ: {e}", exc_info=True)


def update_usdt_rub_rate_in_background():
    """Фоновая задача для обновления курса USDT/RUB в кэше."""
    current_app.logger.info("--- [BG_TASK] Запуск фонового обновления курса USDT/RUB ---")
    try:
        rate = fetch_usdt_rub_rate()
        if rate is not None:
            cache_key = 'currency_rates'
            cache_entry = JsonCache.query.filter_by(cache_key=cache_key).first()
            if not cache_entry:
                cache_entry = JsonCache(cache_key=cache_key)
                db.session.add(cache_entry)

            try:
                rates_data = json.loads(cache_entry.json_data) if cache_entry.json_data else {}
            except (json.JSONDecodeError, TypeError):
                rates_data = {}

            rates_data['USDT'] = str(rate)
            rates_data['USD'] = str(rate)

            cache_entry.json_data = json.dumps(rates_data)
            db.session.commit()
            current_app.logger.info(f"--- [BG_TASK] Курс USDT/RUB успешно обновлен в кэше: {rate}")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"--- [BG_TASK] Ошибка во время фонового обновления курса USDT/RUB: {e}", exc_info=True)