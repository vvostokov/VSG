import hashlib
from flask import current_app
from deep_translator import GoogleTranslator
from models import TranslationCache
from extensions import db

def translate_text(text: str, source: str = 'en', target: str = 'ru') -> str:
    """
    Переводит текст с исходного языка на целевой,
    используя кэш в базе данных, чтобы избежать повторных переводов.
    """
    if not text or not isinstance(text, str):
        return ""

    # Используем MD5 хэш от текста в качестве ключа для кэша
    text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()

    # 1. Сначала проверяем кэш
    cached_translation = TranslationCache.query.filter_by(
        source_hash=text_hash,
        source_lang=source,
        target_lang=target
    ).first()

    if cached_translation:
        current_app.logger.info(f"--- [Translation] Cache HIT for hash: {text_hash}")
        return cached_translation.translated_text

    current_app.logger.info(f"--- [Translation] Cache MISS for hash: {text_hash}. Calling translation API...")

    # 2. Если в кэше нет, переводим
    try:
        translated_text = GoogleTranslator(source=source, target=target).translate(text)
        if not translated_text:
            return text # Возвращаем оригинал в случае пустого ответа

        # 3. Сохраняем в кэш
        new_cache_entry = TranslationCache(
            source_hash=text_hash, source_lang=source, target_lang=target, translated_text=translated_text
        )
        db.session.add(new_cache_entry)
        db.session.commit()
        return translated_text
    except Exception as e:
        current_app.logger.error(f"Ошибка во время перевода: {e}")
        db.session.rollback()
        # В случае ошибки возвращаем оригинальный текст, чтобы не ломать интерфейс
        return text