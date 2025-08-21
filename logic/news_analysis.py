from flask import current_app

# ИЗМЕНЕНО: Импортируем функции из существующих модулей, а не из несуществующего 'services'.
from analytics_logic import get_crypto_portfolio_overview
from news_logic import get_crypto_news

def get_news_trends_for_portfolio(limit=10):
    """
    Анализирует новостные тренды для топ-N активов в портфеле.

    1. Получает общую сводку по крипто-портфелю.
    2. Определяет топ-N активов по их стоимости в RUB.
    3. Для каждого актива из топа получает последние новости и анализирует их тональность.
    4. Агрегирует результаты для отображения.

    Returns:
        tuple: (dict, list) - Словарь с трендами и отсортированный список тикеров.
    """
    try:
        # Эта функция теперь существует в analytics_logic.py
        overview, _ = get_crypto_portfolio_overview()
        if not overview:
            return {}, []
    except Exception as e:
        current_app.logger.error(f"Не удалось получить сводку по портфелю для анализа новостей: {e}")
        return {}, []

    # Сортируем активы по стоимости и берем топ-N
    sorted_assets = sorted(
        overview.items(),
        key=lambda item: item[1].get('total_value_rub', 0),
        reverse=True
    )
    top_tickers = [ticker for ticker, data in sorted_assets[:limit]]

    # Собираем и анализируем новости для каждого тикера
    trends = {}
    for ticker in top_tickers:
        try:
            # ИЗМЕНЕНО: Вызываем существующую функцию get_crypto_news
            articles = get_crypto_news(categories=ticker, limit=30)
            
            positive = sum(1 for a in articles if a.get('sentiment', {}).get('compound', 0) >= 0.05)
            negative = sum(1 for a in articles if a.get('sentiment', {}).get('compound', 0) <= -0.05)
            
            trends[ticker] = {
                'positive': positive,
                'negative': negative,
                'neutral': len(articles) - positive - negative,
                'total': len(articles)
            }
        except Exception as e:
            current_app.logger.error(f"Не удалось получить новости для {ticker}: {e}")
            trends[ticker] = {'positive': 0, 'negative': 0, 'neutral': 0, 'total': 0}
            
    return trends, top_tickers