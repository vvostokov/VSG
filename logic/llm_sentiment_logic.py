import g4f
from flask import current_app
import logging
import inspect

def get_sentiment_g4f(text: str) -> int | None:
    """
    Анализирует тональность текста с помощью g4f и возвращает оценку от -100 до 100.
    Использует автоматический выбор провайдера в библиотеке g4f.

    Args:
        text: Текст для анализа.

    Returns:
        Целое число от -100 до 100 или None в случае ошибки.
    """
    if not text:
        return None

    # Убедимся, что текст не слишком длинный для модели
    max_length = 2000
    if len(text) > max_length:
        text = text[:max_length]

    prompt = f"""
    Проанализируй тональность следующего текста финансовой новости.
    Верни ТОЛЬКО одно целое число от -100 (крайне негативная) до +100 (крайне позитивная).
    0 означает нейтральную тональность. Не добавляй никаких объяснений, знаков процента или дополнительного текста.
    Просто число.

    Текст для анализа:
    ---
    {text}
    ---
    """

    # ИЗМЕНЕНО: Убираем ручной перебор провайдеров и доверяем это библиотеке g4f.
    # Если не указать `provider`, g4f сама попытается найти рабочий.
    try:
        logging.info("--- [g4f] Отправка запроса на анализ тональности (автоматический выбор провайдера)...")
        response = g4f.ChatCompletion.create(
            model=g4f.models.default,
            messages=[{"role": "user", "content": prompt}],
            timeout=40  # Увеличим таймаут, так как библиотека может перебирать несколько провайдеров
        )
        
        logging.info(f"--- [g4f] Получен ответ: '{str(response).strip()}'")
        cleaned_response = ''.join(filter(lambda x: x.isdigit() or x in ['-'], str(response).strip()))
        
        if cleaned_response and cleaned_response != '-':
            return max(-100, min(100, int(cleaned_response)))
        return None
    except Exception as e:
        current_app.logger.error(f"--- [g4f] Ошибка при вызове API для анализа тональности (авто-режим): {e}", exc_info=True)
        return None