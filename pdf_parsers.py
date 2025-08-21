import pdfplumber
import re
from decimal import Decimal, InvalidOperation

def parse_bcs_report_pdf(file_path):
    """
    Парсит PDF-отчет от брокера БКС.

    ВНИМАНИЕ: Эта функция является ШАБЛОНОМ.
    Вам необходимо адаптировать логику извлечения данных (регулярные выражения,
    номера таблиц, индексы колонок) под точную структуру вашего PDF-отчета '123.pdf'.

    Args:
        file_path (str): Путь к PDF-файлу.

    Returns:
        list: Список словарей, где каждый словарь представляет один актив.
              Пример: [{'name': 'Газпром ао', 'ticker': 'GAZP', 'quantity': 100, 'asset_type': 'stock'}]
              В случае ошибки возвращает пустой список.
    """
    assets = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                # Извлекаем текст со страницы для поиска нужных секций
                text = page.extract_text()

                # --- Пример: Поиск таблицы с активами ---
                # Часто отчеты имеют заголовок перед таблицей, например "Портфель ценных бумаг".
                # Используйте его для нахождения нужной таблицы.
                if "Портфель ценных бумаг" in text:
                    # extract_tables() извлекает все таблицы на странице
                    tables = page.extract_tables()
                    
                    # Вам нужно будет определить, какая из таблиц содержит нужные данные.
                    # Это можно сделать по количеству колонок или по заголовкам.
                    # Допустим, нужная нам таблица - первая.
                    if tables:
                        asset_table = tables[0]
                        
                        # Пропускаем заголовок таблицы (например, первую строку)
                        for row in asset_table[1:]:
                            # ВАЖНО: Индексы колонок (0, 1, 2, ...) нужно будет
                            # заменить на правильные для вашего отчета.
                            try:
                                # Пример извлечения данных из колонок
                                name = row[0].strip() if row[0] else ''
                                ticker = row[1].strip() if row[1] else ''
                                quantity = Decimal(row[2].replace(' ', '')) if row[2] else Decimal(0)

                                if not all([name, ticker, quantity > 0]):
                                    continue # Пропускаем неполные или пустые строки

                                assets.append({
                                    'name': name, 'ticker': ticker, 'quantity': quantity,
                                    'asset_type': 'stock' # или 'bond', 'etf'
                                })
                            except (IndexError, InvalidOperation, TypeError) as e:
                                print(f"Ошибка парсинга строки отчета: {row}. Ошибка: {e}")
                                continue
    except Exception as e:
        print(f"Критическая ошибка при парсинге PDF-файла {file_path}: {e}")
        return []

    return assets