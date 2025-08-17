import json
from datetime import datetime, timedelta, timezone
import time
from flask import current_app
import requests
import logging
import feedparser
from concurrent.futures import ThreadPoolExecutor, as_completed

from models import JsonCache
from extensions import db
from api_clients import fetch_cryptocompare_news
from translation_logic import translate_text

# --- Константы ---
NEWS_CACHE_TTL_MINUTES = 30
# ИЗМЕНЕНО: Используем RSS-ленты, сфокусированные на российском рынке.
SECURITIES_RSS_URLS = [
    "https://ru.investing.com/rss/news_301.rss", # Новости - Фондовый рынок - Россия
    "https://ru.investing.com/rss/news_8.rss",   # Новости - Экономические новости - Россия
]

def _fetch_rss_news(feed_url: str, limit: int = 50) -> list:
    """Получает и парсит новости из ОДНОЙ RSS-ленты, используя requests для надежности."""
    try:
        logging.info(f"--- [RSS Fetch] Запрос новостей с {feed_url}...")
        request_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        # 1. Используем requests для получения контента, что решает проблемы с редиректами
        response = requests.get(feed_url, headers=request_headers, timeout=15)
        response.raise_for_status()

        # 2. Передаем полученный контент в feedparser
        feed = feedparser.parse(response.content)
        
        # 3. Проверяем на ошибки парсинга, но не прерываем выполнение
        if feed.bozo:
            # Логируем ошибку, но продолжаем, если хоть что-то удалось распарсить
            logging.warning(f"Ошибка парсинга RSS-ленты (bozo) {feed_url}: {feed.bozo_exception}")
            if not feed.entries:
                return [] # Если ничего не распарсилось, возвращаем пустой список

        logging.info(f"--- [RSS Fetch] Найдено {len(feed.entries)} записей в ленте {feed_url}.")
        if not feed.entries:
            return []

        articles = []
        for entry in feed.entries[:limit]:
            published_dt = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                try:
                    published_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                except (TypeError, ValueError) as e:
                    logging.warning(f"Не удалось преобразовать дату для новости: {entry.get('title')}, ошибка: {e}")

            articles.append({
                'title': entry.get('title', 'Без заголовка'),
                'url': entry.get('link', '#'),
                'body': entry.get('summary', ''),
                'published_dt': published_dt, # datetime object for sorting
                'published_on_str': published_dt.strftime('%d.%m.%Y %H:%M') if published_dt else '',
                'source_info': {'name': feed.feed.get('title', 'RSS Feed')}
            })
        return articles
    except Exception as e:
        logging.error(f"Исключение при обработке RSS-ленты {feed_url}: {e}", exc_info=True)
        return []

def _fetch_multiple_rss_news(feed_urls: list[str], limit: int = 50) -> list:
    """Получает и парсит новости из нескольких RSS-лент, объединяет и сортирует их."""
    all_articles = []

    # Используем ThreadPoolExecutor для параллельной загрузки лент
    with ThreadPoolExecutor(max_workers=len(feed_urls)) as executor:
        future_to_url = {executor.submit(_fetch_rss_news, url, limit=limit): url for url in feed_urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                articles = future.result()
                all_articles.extend(articles)
            except Exception as exc:
                current_app.logger.error(f'--- [RSS Fetch] {url} сгенерировал исключение: {exc}')

    # Сортируем все новости по дате, самые свежие вверху
    all_articles.sort(key=lambda x: x.get('published_dt') or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)

    return all_articles[:limit]

def _get_news_from_cache(cache_key: str, fetch_function, *args, **kwargs):
    """Универсальная функция для получения новостей из кэша или их загрузки."""
    try:
        cache_entry = JsonCache.query.filter_by(cache_key=cache_key).first()
        now_aware = datetime.now(timezone.utc)

        if cache_entry:
            last_updated_from_db = cache_entry.last_updated
            # ИСПРАВЛЕНО: Предотвращаем ошибку 'can't subtract offset-naive and offset-aware datetimes'
            # Если из БД пришло "наивное" время (например, из SQLite), считаем, что оно в UTC.
            if last_updated_from_db and last_updated_from_db.tzinfo is None:
                last_updated_from_db = last_updated_from_db.replace(tzinfo=timezone.utc)

            if last_updated_from_db and (now_aware - last_updated_from_db) < timedelta(minutes=NEWS_CACHE_TTL_MINUTES):
                current_app.logger.info(f"--- [News Cache] Cache HIT for key: {cache_key}")
                return json.loads(cache_entry.json_data)

        current_app.logger.info(f"--- [News Cache] Cache MISS or STALE for key: {cache_key}. Fetching fresh data...")
        
        fresh_news = fetch_function(*args, **kwargs)
        if not fresh_news:
            return json.loads(cache_entry.json_data) if cache_entry else []

        if not cache_entry:
            cache_entry = JsonCache(cache_key=cache_key)
            db.session.add(cache_entry)

        news_to_cache = []
        for article in fresh_news:
            article_copy = article.copy()
            article_copy.pop('published_dt', None)
            news_to_cache.append(article_copy)

        cache_entry.json_data = json.dumps(news_to_cache, default=str)
        cache_entry.last_updated = now_aware
        db.session.commit()
        return fresh_news
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Ошибка при получении/кэшировании новостей для ключа {cache_key}: {e}", exc_info=True)
        # Возвращаем пустой список в случае любой ошибки, чтобы не сломать страницу
        return []

def get_crypto_news(limit: int = 50, categories: str = None):
    """Получает, переводит и кэширует новости о криптовалютах."""
    cache_key = f"crypto_news_translated_{categories or 'all'}"
    
    def fetch_and_translate(limit, categories):
        news_raw = fetch_cryptocompare_news(limit=limit, categories=categories)
        translated = []
        for article in news_raw:
            article['title_ru'] = translate_text(article.get('title', ''))
            article['body_ru'] = translate_text(article.get('body', ''))
            translated.append(article)
        return translated

    return _get_news_from_cache(cache_key, fetch_and_translate, limit=limit, categories=categories)

def get_securities_news(limit: int = 50):
    """Получает и кэширует новости фондового рынка из RSS."""
    cache_key = "securities_news_investing_com_russia" # Обновляем ключ кэша
    return _get_news_from_cache(cache_key, _fetch_multiple_rss_news, feed_urls=SECURITIES_RSS_URLS, limit=limit)