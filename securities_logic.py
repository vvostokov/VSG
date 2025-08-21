import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation

import apimoex
import pandas as pd
import requests
from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for, current_app)
from sqlalchemy import asc, desc
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

# Импортируем модели и db из новых централизованных файлов
from models import InvestmentPlatform, InvestmentAsset, Transaction, MoexHistoricalPrice, HistoricalPriceCache
from extensions import db
from news_logic import get_securities_news
# ИМПОРТ ДЛЯ НОВОЙ ФУНКЦИИ ЗАГРУЗКИ PDF
from pdf_parsers import parse_bcs_report_pdf

# Создаем Blueprint для маршрутов, связанных с ценными бумагами
# ИЗМЕНЕНО: Добавляем префикс /securities для всех маршрутов этого блюпринта для лучшей организации URL.
securities_bp = Blueprint('securities', __name__, url_prefix='/securities', template_folder='templates')

# --- Функции для работы с MOEX API ---

def fetch_moex_securities_metadata(tickers: list[str]) -> dict[str, dict]:
    """
    Получает метаданные (SECID, ISIN, NAME, TYPE) для списка тикеров с MOEX.
    Принимает на вход как SECID, так и ISIN.
    Возвращает словарь, где ключ - исходный тикер из запроса.
    """
    if not tickers:
        return {}

    # Используем один запрос для всех тикеров для эффективности
    metadata = {}
    with requests.Session() as session:
        # apimoex.find_securities может принимать список, но для надежности лучше по одному
        for ticker_query in tickers:
            try:
                print(f"--- [MOEX Meta Fetch] Поиск метаданных для '{ticker_query}'...")
                # Ищем по SECID или ISIN
                data = apimoex.find_securities(session, ticker_query, columns=('secid', 'isin', 'name', 'group', 'primary_boardid'))
                if data:
                    # Берем первую, наиболее релевантную запись
                    sec_info = data[0]
                    print(f"--- [MOEX Meta Fetch DEBUG] Найдены данные для '{ticker_query}': {sec_info}")
                    type_map = {
                        'stock_shares': 'stock',
                        'stock_bonds': 'bond',
                        'stock_etf': 'etf',
                        'stock_ppif': 'etf',
                    }
                    asset_type = type_map.get(sec_info.get('group'), 'other')
                    
                    # Ключом будет исходный запрос (обычно ISIN)
                    metadata[ticker_query] = {
                        'ticker': sec_info.get('secid'), 
                        'isin': sec_info.get('isin'), 
                        'name': sec_info.get('name'), 
                        'asset_type': asset_type,
                        'board': sec_info.get('primary_boardid'),
                        'group': sec_info.get('group') # Сохраняем группу для точного запроса
                    }
            except Exception as e:
                print(f"INFO: Не удалось найти метаданные для '{ticker_query}' на MOEX: {e}")
    return metadata

def fetch_moex_historical_price_range(secids: list[str], start_date: date, end_date: date) -> dict[str, dict[date, Decimal]]:
    """
    Получает диапазон исторических цен закрытия для списка SECID с MOEX.
    Возвращает словарь {secid: {дата: цена}}.
    """
    all_prices = defaultdict(dict)
    with requests.Session() as session:
        for secid in secids:
            try:
                print(f"--- [MOEX History Range] Запрос истории для {secid} с {start_date} по {end_date}...")
                # Запрашиваем данные за весь диапазон
                history = apimoex.get_market_history(
                    session,
                    security=secid,
                    start=start_date.isoformat(),
                    end=end_date.isoformat(),
                    columns=('TRADEDATE', 'CLOSE')
                )
                if history:
                    for record in history:
                        trade_date = datetime.strptime(record['TRADEDATE'], '%Y-%m-%d').date()
                        all_prices[secid][trade_date] = Decimal(str(record['CLOSE']))
            except Exception as e:
                print(f"--- [MOEX History Range] Ошибка при получении истории для {secid}: {e}")
            time.sleep(0.2) # Пауза между запросами по тикерам
    return all_prices

def fetch_moex_historical_prices(isins: list[str], target_date: date) -> dict[str, Decimal]:
    """
    Получает исторические цены закрытия для списка ISIN на конкретную дату.
    Использует кэш в таблице MoexHistoricalPrice.
    """
    prices = {}
    if not isins:
        return prices

    # 1. Проверяем кэш
    cached_prices = MoexHistoricalPrice.query.filter(
        MoexHistoricalPrice.isin.in_(isins),
        MoexHistoricalPrice.date == target_date
    ).all()
    for p in cached_prices:
        prices[p.isin] = p.price_rub

    # 2. Определяем, что нужно запросить у API
    cached_isins = set(prices.keys())
    isins_to_fetch = [isin for isin in isins if isin not in cached_isins]

    # 3. Запрашиваем недостающие данные
    if isins_to_fetch:
        print(f"--- [MOEX History] Запрос исторических цен на {target_date} для {len(isins_to_fetch)} ISIN...")
        with requests.Session() as session:
            for isin in isins_to_fetch:
                try:
                    meta_list = apimoex.find_securities(session, isin, columns=('secid',))
                    if not meta_list: continue
                    secid = meta_list[0]['secid']
                    # Запрашиваем данные за небольшой диапазон до целевой даты, чтобы найти последнюю торговую сессию
                    start_date_for_request = target_date - timedelta(days=7)
                    history = apimoex.get_market_history(
                        session, 
                        security=secid, 
                        start=start_date_for_request.isoformat(), 
                        end=target_date.isoformat(), 
                        columns=('CLOSE',)
                    )
                    if history:
                        # API возвращает данные в хронологическом порядке, берем последнюю запись
                        price = Decimal(str(history[-1]['CLOSE']))
                        prices[isin] = price
                        db.session.add(MoexHistoricalPrice(isin=isin, date=target_date, price_rub=price))
                except Exception as e:
                    print(f"--- [MOEX History] Ошибка при получении исторической цены для {isin} на {target_date}: {e}")
                time.sleep(0.1)
        db.session.commit()
    return prices

def fetch_moex_securities_prices(securities_meta: dict) -> dict[str, Decimal]:
    """
    Получает последние цены для списка ценных бумаг с Московской биржи (MOEX ISS).
    Использует пакетные запросы к доскам с указанием рынка и движка для надежности.
    Корректно рассчитывает "грязную" цену для облигаций.
    """
    if not securities_meta:
        return {}

    print(f"\n--- [MOEX Price Fetch] Начало процесса получения цен (пакетный режим) ---")

    # Этап 1: Группировка по доске, рынку и движку для максимальной точности запроса
    requests_by_key = defaultdict(list)
    secid_to_isin_map = {}
    for isin, meta in securities_meta.items():
        if meta.get('board') and meta.get('ticker') and meta.get('group'):
            secid = meta['ticker'].upper()
            board = meta['board']
            
            # Разбираем группу на движок и рынок
            group_parts = meta['group'].split('_')
            engine, market = 'stock', 'shares' # Значения по умолчанию
            if len(group_parts) == 2:
                engine, market = group_parts
            
            # Особый случай для фондов, где рынок называется 'stock'
            if market in ['etf', 'ppif']:
                market = 'stock'

            requests_by_key[(board, market, engine)].append(secid)
            secid_to_isin_map[secid] = isin
    
    print(f"--- [MOEX Debug] Группировка для запросов: { {f'{k[0]}/{k[1]}/{k[2]}': v for k, v in requests_by_key.items()} }")

    # Этап 2: Сбор и обработка данных
    final_prices = {}
    price_priority = ['LAST', 'MARKETPRICE', 'MARKETPRICE2', 'LCLOSE', 'PREVADMITTEDQUOTE', 'PREVPRICE']
    marketdata_columns = ['SECID', 'LAST', 'MARKETPRICE', 'MARKETPRICE2', 'LCLOSE', 'PREVADMITTEDQUOTE', 'PREVPRICE', 'ACCRUEDINT']
    securities_columns = ['SECID', 'FACEVALUE']

    with requests.Session() as session:
        for (board, market, engine), secids_on_board in requests_by_key.items():
            print(f"\n--- [MOEX Price Fetch] Запрос для: доска='{board}', рынок='{market}', движок='{engine}'...")
            try:
                kwargs = {'market': market, 'engine': engine}
                specs_data = apimoex.get_board_securities(session, board=board, table='securities', columns=securities_columns, **kwargs)
                market_data = apimoex.get_board_securities(session, board=board, table='marketdata', columns=marketdata_columns, **kwargs)
                
                if not market_data:
                    print(f"--- [MOEX Price Fetch] WARNING: Не получены рыночные данные для доски '{board}'.")
                    continue
                
                specs_lookup = {item['SECID']: item for item in specs_data}
                market_lookup = {item['SECID']: item for item in market_data}

                for secid in secids_on_board:
                    specs = specs_lookup.get(secid)
                    market = market_lookup.get(secid)
                    
                    if not market:
                        print(f"--- [MOEX DEBUG] Не найдены рыночные данные для {secid} в ответе от доски '{board}'")
                        continue

                    price_val = next((Decimal(str(market[key])) for key in price_priority if market.get(key) is not None and market.get(key) > 0), None)
                    if not price_val: continue

                    isin = secid_to_isin_map[secid]
                    if securities_meta.get(isin, {}).get('asset_type') == 'bond':
                        if not specs or not specs.get('FACEVALUE'):
                            print(f"--- [MOEX Price Fetch] WARNING: Не найдены спецификации (номинал) для облигации '{secid}'.")
                            continue
                        
                        face_value = specs.get('FACEVALUE')
                        accrued_int = market.get('ACCRUEDINT', '0')
                        dirty_price = (Decimal(str(face_value)) * price_val / Decimal('100')) + Decimal(str(accrued_int))
                        final_prices[isin] = dirty_price
                    else:
                        final_prices[isin] = price_val

            except Exception as e:
                print(f"--- [MOEX Price Fetch] ERROR: Ошибка при обработке доски '{board}': {e}")

    final_not_found = [isin for isin in securities_meta if isin not in final_prices]
    if final_not_found:
        print(f"WARNING: Не удалось найти валидную цену для следующих тикеров MOEX: {final_not_found}")

    print(f"--- [MOEX Price Fetch] Завершение процесса. Итоговый словарь цен: {final_prices}")
    return final_prices

def fetch_moex_market_leaders(tickers: list[str]) -> list[dict]:
    """
    Получает рыночные данные (цена, изменение) для списка ключевых тикеров MOEX.
    """
    if not tickers:
        return []
    
    leaders_data = []
    # Разделим тикеры на акции и индексы для разных запросов
    indices = [t for t in tickers if t.startswith('IMOEX') or t.startswith('RTSI')]
    stocks = [t for t in tickers if t not in indices]

    with requests.Session() as session:
        try:
            # Запрос для акций
            if stocks:
                market_data = apimoex.get_board_securities(
                    session, 
                    board='TQBR', 
                    table='marketdata',
                    columns=('SECID', 'LAST', 'LASTTOPREVPRICE')
                )
                market_lookup = {item['SECID']: item for item in market_data}
                for ticker in stocks:
                    data = market_lookup.get(ticker)
                    if data and data.get('LAST') is not None:
                        leaders_data.append({'ticker': ticker, 'price': Decimal(str(data['LAST'])), 'change_pct': data.get('LASTTOPREVPRICE')})

            # Запрос для индексов
            if indices:
                index_data = apimoex.get_board_securities(
                    session, board='SNDX', table='marketdata',
                    columns=('SECID', 'CURRENTVALUE', 'LASTTOPREVPRICE')
                )
                index_lookup = {item['SECID']: item for item in index_data}
                for ticker in indices:
                    data = index_lookup.get(ticker)
                    if data and data.get('CURRENTVALUE') is not None:
                        leaders_data.append({'ticker': ticker, 'price': Decimal(str(data['CURRENTVALUE'])), 'change_pct': data.get('LASTTOPREVPRICE')})
        except Exception as e:
            print(f"Ошибка при получении данных о лидерах рынка MOEX: {e}")

    return leaders_data
# --- Парсеры брокерских отчетов ---

def _clean_and_convert_to_decimal(value):
    if value is None or pd.isna(value):
        return Decimal('0')
    # Преобразуем значение в строку, убираем пробелы и меняем запятую на точку
    s_value = str(value).strip().replace(' ', '').replace(',', '.')
    try:
        # Decimal() отлично справляется с научной нотацией (например, '1.0e+3')
        return Decimal(s_value)
    except (InvalidOperation, TypeError):
        return Decimal('0')

def _parse_bcs_report(xls_file):
    """
    Парсер для отчетов брокера БКС.
    Ищет лист, похожий на "Портфель по активам", и извлекает данные.
    """
    # Ищем лист с активами. Названия могут варьироваться.
    sheet_name = next((name for name in xls_file.sheet_names if 'портфель' in name.lower() and 'клиент' in name.lower()), None)
    if not sheet_name:
        sheet_name = next((name for name in xls_file.sheet_names if 'портфель' in name.lower()), None)
    
    if not sheet_name:
        return [] # Не нашли подходящий лист

    df = pd.read_excel(xls_file, sheet_name=sheet_name, header=None)

    # Ищем строку с заголовками. Она может быть не на первой строке.
    header_row_index = -1
    for i, row in df.iterrows():
        row_values = [str(cell).strip() for cell in row.values if pd.notna(cell)]
        # Ищем строку, где есть хотя бы несколько ключевых слов из заголовков
        if 'Вид ЦБ' in row_values and 'Наименование ЦБ' in row_values and 'Кол-во' in row_values:
            header_row_index = i
            break
    
    if header_row_index == -1:
        return [] # Не нашли строку с заголовками

    # Читаем данные, используя найденную строку как заголовок
    df = pd.read_excel(xls_file, sheet_name=sheet_name, header=header_row_index)
    df.columns = df.columns.str.strip()

    # Карта возможных названий колонок для гибкости
    column_map = {
        'asset_type_raw': ['Вид ЦБ', 'Тип актива'],
        'name': ['Наименование ЦБ', 'Наименование'],
        'ticker': ['ISIN', 'Код ЦБ'],
        'quantity': ['Кол-во', 'Количество'],
        'price': ['Цена закрытия (расч.)', 'Цена закрытия', 'Рыночная цена'],
        'currency': ['Валюта'],
    }

    actual_columns = {key: next((name for name in names if name in df.columns), None) for key, names in column_map.items()}

    # Проверяем наличие обязательных колонок
    if not all(actual_columns.get(key) for key in ['ticker', 'name', 'quantity']):
        return []

    assets = []
    for _, row in df.iterrows():
        ticker_val = row.get(actual_columns['ticker'])
        if pd.isna(ticker_val) or not str(ticker_val).strip(): continue
        quantity = _clean_and_convert_to_decimal(row.get(actual_columns['quantity']))
        if quantity <= 0: continue
        price = _clean_and_convert_to_decimal(row.get(actual_columns.get('price')))
        currency = str(row.get(actual_columns.get('currency'), 'RUB')).strip()
        name = str(row.get(actual_columns['name'])).strip()
        asset_type_raw = str(row.get(actual_columns.get('asset_type_raw'), '')).lower()
        asset_type = 'bond' if 'облига' in asset_type_raw else ('etf' if 'паи' in asset_type_raw or 'etf' in asset_type_raw else 'stock')

        assets.append({'ticker': str(ticker_val).strip(), 'name': name, 'quantity': quantity, 'current_price': price, 'currency_of_price': currency, 'asset_type': asset_type, 'source_account_type': 'Brokerage'})

    return assets

def _parse_finrez_report(xls_file):
    sheet_name = "Фин.рез."
    if sheet_name not in xls_file.sheet_names: return []
    df = pd.read_excel(xls_file, sheet_name=sheet_name, header=None)
    header_row_index = next((i for i, row in df.iterrows() if 'Валюта' in str(row.values) and 'Инструмент' in str(row.values)), -1)
    if header_row_index == -1: return []
    df = pd.read_excel(xls_file, sheet_name=sheet_name, header=header_row_index)
    df.columns = df.columns.str.strip()
    column_map = {'currency': ['Валюта'], 'asset_type_raw': ['Инструмент'], 'name': ['Актив'], 'total_value': ['Открытая позиция стоимость'], 'quantity': ['Количество', 'Кол-во', 'Количество, шт.', 'Открытая позиция кол-во']}
    actual_columns = {key: next((name for name in names if name in df.columns), None) for key, names in column_map.items()}
    if not all(actual_columns.get(key) for key in ['name', 'total_value', 'quantity']): return []
    df_filtered = df[df[actual_columns['asset_type_raw']].isin(['Акции', 'Облигации'])]
    assets = []
    for _, row in df_filtered.iterrows():
        name_val = row.get(actual_columns['name'])
        if pd.isna(name_val): continue
        quantity = _clean_and_convert_to_decimal(row.get(actual_columns['quantity']))
        total_value = _clean_and_convert_to_decimal(row.get(actual_columns['total_value']))
        if quantity <= 0: continue
        price = total_value / quantity if quantity != 0 else Decimal('0')
        ticker_match = re.search(r'\((.*?)\)', name_val)
        ticker = ticker_match.group(1).strip().upper() if ticker_match else name_val.split(' ')[0].upper()
        asset_type_map = {'Акции': 'stock', 'Облигации': 'bond'}
        asset_type = asset_type_map.get(row.get(actual_columns.get('asset_type_raw')), 'other')
        assets.append({'ticker': ticker, 'name': name_val.strip(), 'quantity': quantity, 'current_price': price, 'currency_of_price': str(row.get(actual_columns.get('currency'), 'RUB')).strip(), 'asset_type': asset_type, 'source_account_type': 'Brokerage'})
    return assets

def _parse_generic_portfolio_report(xls_file):
    try:
        sheet_name = next((name for name in xls_file.sheet_names if any(keyword in name.lower() for keyword in ['портфель', 'активы', 'portfolio', 'assets'])), None)
        if not sheet_name: return []
        df = pd.read_excel(xls_file, sheet_name=sheet_name, header=None)
        header_keywords = ['Код финансового инструмента', 'Тикер', 'Наименование инструмента', 'Эмитент', 'Актив', 'Инструмент', 'Код актива', 'Symbol']
        header_row_index = next((i for i, row in df.iterrows() if any(keyword in str(cell) for keyword in header_keywords for cell in row.values if pd.notna(cell))), -1)
        if header_row_index == -1: return []
        df = pd.read_excel(xls_file, sheet_name=sheet_name, header=header_row_index)
        df.columns = df.columns.str.strip()
        column_map = {'ticker': ['Код финансового инструмента', 'Тикер', 'Код актива', 'Symbol'], 'name': ['Эмитент', 'Наименование инструмента', 'Наименование', 'Актив', 'Инструмент', 'Name'], 'quantity': ['Количество, шт.', 'Количество', 'Кол-во', 'Остаток', 'Quantity'], 'current_price': ['Цена закрытия', 'Рыночная цена', 'Цена последней сделки', 'Цена послед.', 'Текущая цена', 'Price'], 'currency_of_price': ['Валюта цены', 'Валюта', 'Currency'], 'asset_type': ['Тип ЦБ', 'Тип актива', 'Тип инструмента', 'Asset Type']}
        actual_columns = {key: next((name for name in potential_names if name in df.columns), None) for key, potential_names in column_map.items()}
        if 'ticker' not in actual_columns or 'quantity' not in actual_columns: raise ValueError(f"Не найдены обязательные колонки ('Тикер' и 'Количество').")
        assets = []
        for index, row in df.iterrows():
            ticker_val = row.get(actual_columns['ticker'])
            if pd.isna(ticker_val): continue
            quantity_val = _clean_and_convert_to_decimal(row.get(actual_columns.get('quantity')))
            if quantity_val <= 0: continue
            price_val = _clean_and_convert_to_decimal(row.get(actual_columns.get('current_price')))
            assets.append({'ticker': str(ticker_val).strip(), 'name': str(row.get(actual_columns.get('name'), ticker_val)).strip(), 'quantity': quantity_val, 'current_price': price_val, 'currency_of_price': str(row.get(actual_columns.get('currency_of_price'), 'RUB')).strip(), 'asset_type': str(row.get(actual_columns.get('asset_type'), 'stock')).lower().strip(), 'source_account_type': 'Brokerage'})
        return assets
    except Exception as e:
        raise type(e)(f"Ошибка при обработке файла отчета: {e}")

def _parse_dinamika_pozitsiy_report(xls_file):
    sheet_name = "Динамика позиций"
    if sheet_name not in xls_file.sheet_names: return []
    df = pd.read_excel(xls_file, sheet_name=sheet_name, header=None)
    header_row_index = next((i for i, row in df.iterrows() if 'Инструмент' in str(row.values) and ('Код инструмента' in str(row.values) or 'ISIN' in str(row.values))), -1)
    if header_row_index == -1: return []
    df.columns = [str(col).strip() for col in df.iloc[header_row_index]]
    df = df.iloc[header_row_index + 1:].reset_index(drop=True)
    column_map = {'ticker': ['Код инструмента', 'ISIN'], 'name': ['Инструмент'], 'quantity': ['Количество на конец периода', 'Конечный остаток, шт']}
    actual_columns = {key: next((name for name in names if name in df.columns), None) for key, names in column_map.items()}
    if not all(actual_columns.get(key) for key in ['ticker', 'name', 'quantity']): return []
    assets = []
    QUANTIZER = Decimal('1.000000')
    for _, row in df.iterrows():
        name_val = row.get(actual_columns['name'])
        if pd.isna(name_val) or not str(name_val).strip(): continue
        quantity = _clean_and_convert_to_decimal(row.get(actual_columns['quantity']))
        if quantity <= 0: continue
        ticker_val = str(row.get(actual_columns['ticker'])).strip()
        quantized_quantity = quantity.quantize(QUANTIZER)
        assets.append({'ticker': ticker_val, 'name': str(name_val).strip(), 'quantity': quantized_quantity, 'current_price': Decimal('0'), 'currency_of_price': 'RUB', 'asset_type': 'stock', 'source_account_type': 'Brokerage'})
    return assets

def _parse_broker_portfolio_report(file_path):
    engine = 'openpyxl' if file_path.endswith('.xlsx') else 'xlrd'
    xls = pd.ExcelFile(file_path, engine=engine)
    # ИЗМЕНЕНО: Добавляем новый парсер для БКС в начало списка
    for parser_func in [_parse_bcs_report, _parse_dinamika_pozitsiy_report, _parse_generic_portfolio_report, _parse_finrez_report]:
        try:
            assets = parser_func(xls)
            if assets:
                current_app.logger.info(f"--- [Parser] Отчет успешно разобран с помощью: {parser_func.__name__}")
                return assets
        except Exception as e:
            current_app.logger.warning(f"--- [Parser] Ошибка при использовании парсера {parser_func.__name__}: {e}")
            continue # Пробуем следующий парсер
    return []

def _parse_bcs_universal_deals_report(xls_file):
    """
    Универсальный и более надежный парсер для отчетов по сделкам от брокера БКС.
    Он объединяет логику предыдущих парсеров и добавляет гибкости.
    - Ищет раздел со сделками по разным ключевым словам.
    - Гибко находит заголовок таблицы и сопоставляет колонки.
    - Обрабатывает отчеты, где сделки по разным активам сгруппированы.
    - Игнорирует секции, не связанные с ЦБ (например, фьючерсы).
    """
    current_app.logger.info("--- [BCS Universal Deals Parser] Начало работы...")

    # Ключевые слова для поиска начала раздела
    section_start_keywords = ['2.1. Сделки:', 'Сделки купли/продажи ЦБ']
    # Ключевые слова для поиска конца раздела
    section_end_keywords = [r'^\s*3\.\s*Активы:', r'^\s*2\.2\.\s*']
    # Ключевые слова для игнорирования секций
    ignore_section_keywords = ['Инструменты срочного рынка']

    # Карта для гибкого сопоставления колонок
    column_map = {
        'id': ['Номер', 'Номер сделки'],
        'date': ['Дата'],
        'time': ['Время соверш.', 'Время сделки', 'Время'],
        'buy_qty': ['Куплено, шт'],
        'buy_price': ['Цена'], # Цена покупки
        'buy_sum': ['Сумма платежа'],
        'sell_qty': ['Продано, шт'],
        'sell_price': ['Цена'], # Цена продажи
        'sell_sum': ['Сумма выручки'],
        'currency': ['Валюта'],
        'fee': ['Комиссия Брокера', 'Комиссия'],
    }

    all_transactions = []

    for sheet_name in xls_file.sheet_names:
        try:
            df = pd.read_excel(xls_file, sheet_name=sheet_name, header=None)
            if df.empty:
                continue
            
            in_deals_section = False
            in_ignored_section = False
            current_asset_info = None
            header_indices = {}

            for i, row in df.iterrows():
                row = df.iloc[i]
                row_str = ' '.join([str(cell).strip() for cell in row if pd.notna(cell)])
                if not row_str: continue

                # Проверяем, не вошли ли мы в секцию фьючерсов
                if any(re.search(keyword, row_str, re.IGNORECASE) for keyword in ignore_section_keywords):
                    current_app.logger.info(f"--- [BCS Universal] Обнаружена игнорируемая секция: '{row_str}'")
                    in_ignored_section = True
                    in_deals_section = False # Выходим из секции сделок, если она была активна
                    current_asset_info = None
                    header_indices = {}
                    continue
                
                # Проверяем, не закончился ли раздел
                if in_deals_section and any(re.search(keyword, row_str) for keyword in section_end_keywords):
                    current_app.logger.info(f"--- [BCS Universal] Обнаружен конец раздела сделок: '{row_str}'")
                    in_deals_section = False
                    break # Переходим к следующему листу

                # Ищем начало раздела сделок
                if not in_deals_section and any(keyword in row_str for keyword in section_start_keywords):
                    current_app.logger.info(f"--- [BCS Universal] Найден раздел сделок на листе '{sheet_name}': '{row_str}'")
                    in_deals_section = True
                    in_ignored_section = False # Сбрасываем флаг игнорирования
                    continue

                if not in_deals_section or in_ignored_section:
                    continue

                # Внутри раздела сделок ищем информацию об активе
                isin_match = re.search(r'ISIN:\s*([A-Z0-9]+)', row_str)
                if isin_match:
                    name_candidate = str(row.iloc[7]).strip() if len(row) > 7 and pd.notna(row.iloc[7]) and str(row.iloc[7]).strip() else isin_match.group(1)
                    current_asset_info = {'isin': isin_match.group(1), 'name': name_candidate}
                    header_indices = {} # Сбрасываем заголовки для нового актива
                    current_app.logger.info(f"--- [BCS Universal] Найден актив: {current_asset_info}")
                    continue

                # Ищем заголовок таблицы
                header_row_list = [str(c).strip() for c in row]
                # Проверяем, что это заголовок, по наличию ключевых колонок
                if 'Дата' in header_row_list and 'Куплено, шт' in header_row_list and 'Продано, шт' in header_row_list:
                    header_indices = {}
                    # Find all indices for 'Цена'
                    price_indices = [idx for idx, col_name in enumerate(header_row_list) if col_name == 'Цена']
                    
                    # Map other columns
                    for key, names in column_map.items():
                        if key in ['buy_price', 'sell_price']: continue # Skip price for now
                        for name in names:
                            try:
                                header_indices[key] = header_row_list.index(name)
                                break
                            except ValueError:
                                continue
                    
                    # Map price columns specifically
                    if len(price_indices) > 0:
                        header_indices['buy_price'] = price_indices[0]
                    if len(price_indices) > 1:
                        header_indices['sell_price'] = price_indices[1]

                    current_app.logger.info(f"--- [BCS Universal] Найден и обработан заголовок таблицы. Индексы: {header_indices}")
                    continue

                # Парсим строку транзакции, если есть информация об активе и заголовках
                first_cell = row.iloc[0]
                is_transaction_row = False
                if pd.notna(first_cell):
                    if isinstance(first_cell, datetime):
                        is_transaction_row = True
                    elif isinstance(first_cell, str) and (re.match(r'\d{2}\.\d{2}\.\d{4}', first_cell.strip()) or re.match(r'\d{2}\.\d{2}\.\d{2}', first_cell.strip())):
                        is_transaction_row = True

                if current_asset_info and header_indices and 'date' in header_indices and is_transaction_row:
                    try:
                        buy_qty = _clean_and_convert_to_decimal(row.iloc[header_indices['buy_qty']])
                        sell_qty = _clean_and_convert_to_decimal(row.iloc[header_indices['sell_qty']])
                        
                        if buy_qty > 0:
                            trade_type, quantity, price, total_sum = 'buy', buy_qty, _clean_and_convert_to_decimal(row.iloc[header_indices['buy_price']]), _clean_and_convert_to_decimal(row.iloc[header_indices['buy_sum']])
                        elif sell_qty > 0:
                            trade_type, quantity, price, total_sum = 'sell', sell_qty, _clean_and_convert_to_decimal(row.iloc[header_indices['sell_price']]), _clean_and_convert_to_decimal(row.iloc[header_indices['sell_sum']])
                        else:
                            continue
                            
                        date_val = row.iloc[header_indices['date']]
                        time_val = row.iloc[header_indices['time']]
                        trade_date = pd.to_datetime(date_val).date()
                        trade_time = pd.to_datetime(time_val).time()
                        timestamp = datetime.combine(trade_date, trade_time).replace(tzinfo=timezone.utc)
                        
                        fee = _clean_and_convert_to_decimal(row.iloc[header_indices['fee']]) if 'fee' in header_indices and header_indices['fee'] is not None else Decimal('0')
                        currency = str(row.iloc[header_indices['currency']]).strip()

                        all_transactions.append({
                            'exchange_tx_id': f"bcs_deal_{str(row.iloc[header_indices['id']]).strip()}", 'timestamp': timestamp, 'type': trade_type,
                            'raw_type': f"Сделка {trade_type}", 'asset1_ticker': current_asset_info['isin'], 'asset1_amount': quantity,
                            'asset2_ticker': currency, 'asset2_amount': total_sum, 'execution_price': price,
                            'fee_amount': fee, 'fee_currency': currency, 'description': f"BCS deal for {current_asset_info['name']}"
                        })
                    except Exception as e:
                        current_app.logger.warning(f"--- [BCS Universal] Ошибка при обработке строки транзакции (строка {i+1}): {e}")

            if all_transactions:
                current_app.logger.info(f"--- [BCS Universal] Обработка листа '{sheet_name}' завершена. Найдено транзакций: {len(all_transactions)}")
                # Если нашли транзакции на одном листе, считаем, что это нужный файл и выходим
                return all_transactions

        except Exception as e:
            current_app.logger.error(f"--- [BCS Universal] Критическая ошибка при обработке листа '{sheet_name}': {e}", exc_info=True)
            continue

    current_app.logger.warning("--- [BCS Universal] Подходящий лист для этого парсера не найден во всем файле.")
    return all_transactions

def _parse_generic_transactions_report(xls_file):
    """
    Универсальный парсер для отчетов по сделкам.
    Ищет лист с ключевыми словами "сделки", "операции".
    """
    try:
        # ИЗМЕНЕНО: Расширяем список ключевых слов для поиска листа с транзакциями
        primary_keywords = [
            'завершенные сделки', 'торговые операции', 'сделки купли/продажи цб', 
            'отчет по сделкам', 'движение по ценным бумагам'
        ]
        secondary_keywords = ['сделки', 'transactions', 'операции с цб']

        sheet_name = next((name for name in xls_file.sheet_names if any(k in name.lower() for k in primary_keywords)), None)
        if not sheet_name:
            sheet_name = next((name for name in xls_file.sheet_names if any(k in name.lower() for k in secondary_keywords) and 'репо' not in name.lower()), None)
        
        if not sheet_name: 
            raise ValueError("Не найден лист с транзакциями. Проверьте, что название листа содержит ключевые слова (например, 'Сделки', 'Операции').")
        
        current_app.logger.info(f"--- [Generic Parser] Найден лист с транзакциями: '{sheet_name}'")
        df_raw = pd.read_excel(xls_file, sheet_name=sheet_name).dropna(how='all').dropna(axis=1, how='all').reset_index(drop=True)
        column_map = {'trade_id': ['№ сделки', 'Номер сделки'], 'trade_date': ['Дата сделки', 'Дата заключен.'], 'trade_time': ['Время', 'Время сделки', 'Время заключ.'], 'trade_type': ['Вид сделки', 'Тип сделки', 'Операция', 'Тип операции'], 'ticker': ['Инструмент', 'Тикер', 'Код актива', 'ISIN/рег.код'], 'name': ['Актив'], 'quantity': ['Кол-во', 'Количество, шт.', 'Количество', 'Количество актива', 'Количество актива⁷, шт./грамм'], 'price': ['Цена', 'Цена сделки'], 'total_sum': ['Сумма сделки', 'Сумма', 'Сумма сделки в валюте расчетов', 'Сумма сделки в валюте расчетов⁸'], 'currency': ['Валюта цены', 'Валюта', 'Валюта расчетов'], 'broker_fee': ['Комиссия брокера', 'Ком. брокера', 'Комиссия банка'], 'exchange_fee': ['Комиссия биржи', 'Ком. биржи'], 'fee_currency': ['Валюта комиссии'], 'comment': ['Коммент.', 'Комментарий']}
        all_header_keywords = [name for names in column_map.values() for name in names]
        header_row_index = -1
        for i, row in df_raw.iterrows():
            row_values = [str(cell).replace('\n', ' ').strip() for cell in row.values if pd.notna(cell)]
            matches = sum(1 for cell_value in row_values for keyword in all_header_keywords if keyword in cell_value)
            if matches >= 4:
                header_row_index = i
                break
        if header_row_index == -1: raise ValueError(f"Не удалось найти заголовок таблицы транзакций.")
        df_raw.columns = [str(col).replace('\n', ' ').strip() if pd.notna(col) else f'unnamed_{i}' for i, col in enumerate(df_raw.iloc[header_row_index])]
        df = df_raw.iloc[header_row_index + 1:].reset_index(drop=True)
        actual_columns = {key: next((name for name in potential_names if name in df.columns), None) for key, potential_names in column_map.items()}
        if any(key not in actual_columns for key in ['trade_id', 'ticker', 'quantity', 'price', 'trade_date']): raise ValueError("Не найдены обязательные колонки в отчете о сделках.")
        df = df.dropna(subset=[actual_columns['trade_id']])
        transactions = []
        for index, row in df.iterrows():
            try:
                date_cell_value = str(row[actual_columns['trade_date']]).replace('\n', ' ').strip().split()[0]
                time_str_candidate = str(row.get(actual_columns.get('trade_time'), '00:00:00')).replace('\n', ' ').strip().split()[0]
                time_str = '00:00:00' if pd.isna(pd.to_datetime(time_str_candidate, errors='coerce')) else time_str_candidate
                timestamp = datetime.combine(pd.to_datetime(date_cell_value, dayfirst=True).date(), pd.to_datetime(time_str).time()).replace(tzinfo=timezone.utc)
                quantity_raw = _clean_and_convert_to_decimal(row[actual_columns['quantity']])
                trade_type_raw = str(row.get(actual_columns.get('trade_type'), 'N/A'))
                trade_type = 'buy' if 'покупка' in trade_type_raw.lower() or quantity_raw > 0 else ('sell' if 'продажа' in trade_type_raw.lower() or quantity_raw < 0 else None)
                if not trade_type: continue
                if 'репо' in str(row.get(actual_columns.get('comment'), '')).lower(): continue
                quantity = abs(quantity_raw)
                price = _clean_and_convert_to_decimal(row[actual_columns['price']])
                total_sum = _clean_and_convert_to_decimal(row.get(actual_columns.get('total_sum'), quantity * price))
                total_fee = _clean_and_convert_to_decimal(row.get(actual_columns.get('broker_fee'), 0)) + _clean_and_convert_to_decimal(row.get(actual_columns.get('exchange_fee'), 0))
                currency_val = str(row.get(actual_columns.get('currency'), 'RUB')).strip()
                transactions.append({'exchange_tx_id': f"broker_trade_{row[actual_columns['trade_id']]}", 'timestamp': timestamp, 'type': trade_type, 'raw_type': trade_type_raw, 'asset1_ticker': row[actual_columns['ticker']].strip(), 'asset1_amount': quantity, 'asset2_ticker': currency_val, 'asset2_amount': total_sum, 'execution_price': price, 'fee_amount': total_fee, 'fee_currency': currency_val, 'description': f"Broker trade {row.get(actual_columns.get('name'), '').strip()}"})
            except Exception as e:
                print(f"Ошибка при обработке строки транзакции {index}: {e}")
                continue
        return transactions
    except Exception as e:
        raise type(e)(f"Ошибка при обработке файла отчета о транзакциях: {e}") from e

def _parse_broker_transactions_report(file_path):
    """
    Диспетчер парсеров отчетов по транзакциям. Пробует разные парсеры по очереди.
    """
    engine = 'openpyxl' if file_path.endswith('.xlsx') else 'xlrd'
    xls = pd.ExcelFile(file_path, engine=engine)
    # ИЗМЕНЕНО: Порядок парсеров. Сначала пробуем новый универсальный парсер для БКС, затем общий.    
    for parser_func in [_parse_bcs_universal_deals_report, _parse_generic_transactions_report]:
        try:
            transactions = parser_func(xls)
            if transactions:
                current_app.logger.info(f"--- [Parser] Отчет о транзакциях успешно разобран с помощью: {parser_func.__name__}")
                return transactions
        except Exception as e:
            current_app.logger.warning(f"--- [Parser] Ошибка при использовании парсера транзакций {parser_func.__name__}: {e}")
            continue
    return []

# --- Маршруты (Views) ---

@securities_bp.route('/upload-report', methods=['GET', 'POST'])
def ui_upload_securities_report():
    """
    Страница для загрузки отчетов брокера (PDF, XLS, XLSX).
    Обрабатывает PDF отчеты.
    """
    if request.method == 'POST':
        platform_id = request.form.get('platform_id')
        if not platform_id:
            flash('Пожалуйста, выберите брокерскую платформу.', 'danger')
            return redirect(request.url)

        if 'report_file' not in request.files:
            flash('Файл не был выбран.', 'danger')
            return redirect(request.url)

        file = request.files['report_file']
        if file.filename == '':
            flash('Файл не был выбран.', 'danger')
            return redirect(request.url)

        # Обрабатываем только PDF файлы, так как Excel загружается на другой странице
        if file and file.filename.endswith('.pdf'):
            filename = secure_filename(file.filename)
            upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
            file_path = os.path.join(upload_folder, filename)
            file.save(file_path)

            parsed_assets = parse_bcs_report_pdf(file_path)
            os.remove(file_path)

            if not parsed_assets:
                flash('Не удалось извлечь данные из PDF-отчета. Проверьте формат файла или лог ошибок в консоли.', 'danger')
                return redirect(request.url)

            updated_count, created_count = 0, 0
            for parsed_asset in parsed_assets:
                existing_asset = InvestmentAsset.query.filter_by(platform_id=platform_id, ticker=parsed_asset['ticker']).first()
                if existing_asset:
                    existing_asset.quantity = parsed_asset['quantity']
                    updated_count += 1
                else:
                    db.session.add(InvestmentAsset(platform_id=platform_id, ticker=parsed_asset['ticker'], name=parsed_asset['name'], asset_type=parsed_asset['asset_type'], quantity=parsed_asset['quantity'], current_price=Decimal(0), currency_of_price='RUB'))
                    created_count += 1
            
            try:
                db.session.commit()
                flash(f'Импорт из PDF успешно завершен. Обновлено: {updated_count}, Создано: {created_count} активов.', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Ошибка при сохранении данных в БД: {e}', 'danger')

            return redirect(url_for('securities.ui_securities_assets'))

    broker_platforms = InvestmentPlatform.query.filter_by(platform_type='stock_broker', is_active=True).all()
    return render_template('securities/upload_report.html', platforms=broker_platforms)

@securities_bp.route('/brokers')
def ui_brokers():
    broker_platforms = InvestmentPlatform.query.filter_by(platform_type='stock_broker').order_by(InvestmentPlatform.name).all()
    return render_template('brokers.html', platforms=broker_platforms)

@securities_bp.route('/brokers/<int:platform_id>')
def ui_broker_detail(platform_id):
    platform = InvestmentPlatform.query.filter_by(id=platform_id, platform_type='stock_broker').first_or_404()
    currency_rates_to_rub = {'RUB': Decimal('1.0'), 'USD': Decimal('90.0'), None: Decimal('1.0')}
    valued_assets = []
    platform_total_value_rub = Decimal(0)
    assets_with_balance = platform.assets.filter(InvestmentAsset.quantity > 0).order_by(InvestmentAsset.name)
    for asset in assets_with_balance:
        asset_value_rub = (asset.quantity or 0) * (asset.current_price or 0) * currency_rates_to_rub.get(asset.currency_of_price, Decimal('1.0'))
        platform_total_value_rub += asset_value_rub
        valued_assets.append({'asset': asset, 'value_rub': asset_value_rub})
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort_by', 'timestamp')
    order = request.args.get('order', 'desc')
    sort_column = getattr(Transaction, sort_by, Transaction.timestamp)
    transactions_query = platform.transactions.order_by(desc(sort_column) if order == 'desc' else asc(sort_column))
    transactions_pagination = transactions_query.paginate(page=page, per_page=15, error_out=False)
    return render_template('broker_detail.html', platform=platform, valued_assets=valued_assets, platform_total_value_rub=platform_total_value_rub, platform_transactions=transactions_pagination.items, transactions_pagination=transactions_pagination, sort_by=sort_by, order=order)

@securities_bp.route('/brokers/<int:platform_id>/assets/add', methods=['GET', 'POST'])
def ui_add_security_asset_form(platform_id):
    platform = InvestmentPlatform.query.filter_by(id=platform_id, platform_type='stock_broker').first_or_404()
    if request.method == 'POST':
        try:
            ticker_or_isin = request.form.get('ticker', '').upper().strip()
            name = request.form.get('name', '').strip()
            if not ticker_or_isin:
                raise ValueError('Тикер (SECID или ISIN) является обязательным полем.')
            metadata_response = fetch_moex_securities_metadata([ticker_or_isin])
            asset_meta = metadata_response.get(ticker_or_isin)
            if not asset_meta or not asset_meta.get('isin'):
                raise ValueError(f'Не удалось найти метаданные для "{ticker_or_isin}" на Московской бирже.')
            
            asset_isin = asset_meta['isin']
            name = name or asset_meta.get('name', asset_isin)

            current_price_str = request.form.get('current_price', '')
            if current_price_str and current_price_str.strip():
                current_price = Decimal(current_price_str)
            else:
                fetched_prices = fetch_moex_securities_prices(securities_meta=metadata_response)
                current_price = fetched_prices.get(asset_isin, Decimal('0'))
                if current_price > 0:
                    flash(f'Цена для {asset_isin} была автоматически получена с MOEX.', 'info')
                else:
                    flash(f'Не удалось автоматически получить цену для {asset_isin}. Пожалуйста, введите ее вручную.', 'warning')
            
            quantity = Decimal(request.form.get('quantity', '0'))
            existing_asset = InvestmentAsset.query.filter_by(platform_id=platform.id, ticker=asset_isin).first()
            if existing_asset:
                existing_asset.quantity += quantity
                existing_asset.current_price = current_price
                existing_asset.name = name
                flash(f'Актив "{asset_isin}" обновлен: добавлено {quantity} шт., цена и название обновлены.', 'success')
            else:
                new_asset = InvestmentAsset(ticker=asset_isin, name=name, asset_type=asset_meta.get('asset_type', 'stock'), quantity=quantity, current_price=current_price, currency_of_price=request.form.get('currency_of_price', 'RUB'), platform_id=platform.id, source_account_type='Brokerage')
                db.session.add(new_asset)
                flash(f'Актив "{name}" ({asset_isin}) успешно добавлен.', 'success')
            db.session.commit()
            return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))
        except (InvalidOperation, ValueError, Exception) as e:
            db.session.rollback()
            flash(f'Ошибка: {e}', 'danger')
            return render_template('add_security_asset.html', platform=platform, current_data=request.form)
    return render_template('add_security_asset.html', platform=platform, current_data={})

@securities_bp.route('/assets/<int:asset_id>/edit', methods=['GET', 'POST'])
def ui_edit_security_asset(asset_id):
    asset = InvestmentAsset.query.get_or_404(asset_id)
    if asset.platform.platform_type != 'stock_broker':
        flash('Этот актив не является ценной бумагой.', 'danger')
        return redirect(url_for('main.ui_investment_platform_detail', platform_id=asset.platform_id))
    if request.method == 'POST':
        try:
            asset.quantity = Decimal(request.form.get('quantity', '0'))
            asset.ticker = request.form.get('ticker', asset.ticker).upper().strip()
            asset.name = request.form.get('name', asset.name).strip()
            asset.current_price = Decimal(request.form.get('current_price', '0'))
            asset.currency_of_price = request.form.get('currency_of_price', asset.currency_of_price)
            db.session.commit()
            flash(f'Актив "{asset.ticker}" успешно обновлен.', 'success')
            return redirect(url_for('securities.ui_broker_detail', platform_id=asset.platform_id))
        except (InvalidOperation, Exception) as e:
            db.session.rollback()
            flash(f'Ошибка при обновлении: {e}', 'danger')
        return render_template('edit_security_asset.html', asset=asset)
    return render_template('edit_security_asset.html', asset=asset)

@securities_bp.route('/assets/<int:asset_id>/delete', methods=['POST'])
def ui_delete_security_asset(asset_id):
    asset = InvestmentAsset.query.get_or_404(asset_id)
    if asset.platform.platform_type != 'stock_broker':
        flash('Этот актив не является ценной бумагой.', 'danger')
        return redirect(url_for('main.ui_investment_platform_detail', platform_id=asset.platform_id))
    platform_id = asset.platform_id
    db.session.delete(asset)
    db.session.commit()
    flash(f'Актив "{asset.ticker}" удален.', 'success')
    return redirect(url_for('securities.ui_broker_detail', platform_id=platform_id))

@securities_bp.route('/brokers/<int:platform_id>/sync_prices', methods=['POST'])
def ui_sync_broker_prices(platform_id):
    platform = InvestmentPlatform.query.filter_by(id=platform_id, platform_type='stock_broker').first_or_404()
    assets_to_update = platform.assets.filter(InvestmentAsset.quantity > 0).all()
    if not assets_to_update:
        flash('Нет активов для обновления цен.', 'info')
        return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))
    try:
        # Теперь asset.ticker всегда должен быть ISIN
        tickers_to_query = [asset.ticker for asset in assets_to_update]
        securities_metadata = fetch_moex_securities_metadata(tickers_to_query)
        
        fetched_prices_by_isin = fetch_moex_securities_prices(securities_meta=securities_metadata)
        
        if not fetched_prices_by_isin:
            raise ValueError('Не удалось получить цены с Московской биржи.')
            
        updated_count = 0
        for asset in assets_to_update:
            if asset.ticker in fetched_prices_by_isin:
                asset.current_price = fetched_prices_by_isin[asset.ticker]
                asset.currency_of_price = 'RUB'
                updated_count += 1
        if updated_count > 0:
            db.session.commit()
            flash(f'Цены для {updated_count} активов успешно обновлены.', 'success')
        else:
            flash('Все цены уже актуальны.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Произошла ошибка при обновлении цен: {e}', 'danger')
    return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))

@securities_bp.route('/brokers/<int:platform_id>/upload_report', methods=['POST'])
def ui_upload_broker_report(platform_id):
    platform = InvestmentPlatform.query.filter_by(id=platform_id, platform_type='stock_broker').first_or_404()
    file = request.files.get('broker_report')
    if not file or not file.filename:
        flash('Файл не был выбран.', 'danger')
        return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))
    if not (file.filename.endswith('.xls') or file.filename.endswith('.xlsx')):
        flash('Допускаются только файлы формата .xls или .xlsx', 'danger')
        return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))
    
    filename = secure_filename(file.filename)
    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
    try:
        file.save(filepath)
        parsed_assets = _parse_broker_portfolio_report(filepath)
        if not parsed_assets:
            raise ValueError("Не удалось извлечь ни одного актива из файла. Проверьте формат отчета.")
        
        # ИЗМЕНЕНО: Логика обновления активов. Вместо полного удаления,
        # активы обновляются, добавляются или обнуляются.
        existing_assets = {asset.ticker: asset for asset in platform.assets}
        report_tickers = set()
        updated_count, added_count, zeroed_count = 0, 0, 0

        for asset_data in parsed_assets:
            ticker = asset_data['ticker']
            report_tickers.add(ticker)
            
            if ticker in existing_assets:
                existing_asset = existing_assets[ticker]
                existing_asset.quantity = asset_data['quantity']
                existing_asset.current_price = asset_data.get('current_price', existing_asset.current_price)
                existing_asset.name = asset_data.get('name', existing_asset.name)
                updated_count += 1
            else:
                db.session.add(InvestmentAsset(platform_id=platform.id, **asset_data))
                added_count += 1

        for ticker, asset in existing_assets.items():
            if ticker not in report_tickers:
                asset.quantity = Decimal('0')
                zeroed_count += 1

        db.session.commit()
        flash(f'Отчет успешно загружен. Добавлено: {added_count}, Обновлено: {updated_count}, Обнулено: {zeroed_count}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при обработке файла: {e}', 'danger')
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
    return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))

@securities_bp.route('/brokers/<int:platform_id>/upload_transactions_report', methods=['POST'])
def ui_upload_broker_transactions_report(platform_id):
    platform = InvestmentPlatform.query.filter_by(id=platform_id, platform_type='stock_broker').first_or_404()
    file = request.files.get('transactions_report')
    if not file or not file.filename:
        flash('Файл не был выбран.', 'danger')
        return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))
    if not (file.filename.endswith('.xls') or file.filename.endswith('.xlsx')):
        flash('Допускаются только файлы формата .xls или .xlsx', 'danger')
        return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))

    filename = secure_filename(file.filename)
    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
    try:
        file.save(filepath)
        parsed_txs = _parse_broker_transactions_report(filepath)
        if not parsed_txs:
            raise ValueError("Не удалось извлечь ни одной транзакции из файла.")
        existing_tx_ids = {tx.exchange_tx_id for tx in platform.transactions}
        added_count = 0
        for tx_data in parsed_txs:
            if tx_data['exchange_tx_id'] not in existing_tx_ids:
                db.session.add(Transaction(platform_id=platform.id, **tx_data))
                added_count += 1
        db.session.commit()
        flash(f'Отчет о транзакциях успешно загружен. Добавлено {added_count} новых сделок.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при обработке файла транзакций: {e}', 'danger')
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
    return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))

@securities_bp.route('/brokers/<int:platform_id>/calculate_assets', methods=['POST'])
def ui_calculate_broker_assets_from_transactions(platform_id):
    platform = InvestmentPlatform.query.filter_by(id=platform_id, platform_type='stock_broker').first_or_404()
    try:
        transactions = platform.transactions.order_by(Transaction.timestamp.asc()).all()
        if not transactions:
            flash('Нет транзакций для расчета активов.', 'warning')
            return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))

        holdings = defaultdict(lambda: {'quantity': Decimal(0)})
        for tx in transactions:
            if tx.type == 'buy' and tx.asset1_ticker:
                holdings[tx.asset1_ticker]['quantity'] += tx.asset1_amount
            elif tx.type == 'sell' and tx.asset1_ticker:
                holdings[tx.asset1_ticker]['quantity'] -= tx.asset1_amount
            elif tx.type == 'exchange':
                if tx.asset1_ticker: holdings[tx.asset1_ticker]['quantity'] -= tx.asset1_amount
                if tx.asset2_ticker: holdings[tx.asset2_ticker]['quantity'] += tx.asset2_amount
        
        final_holdings_by_isin = {isin: data for isin, data in holdings.items() if data['quantity'] > 0}
        if not final_holdings_by_isin:
            InvestmentAsset.query.filter_by(platform_id=platform.id).delete()
            db.session.commit()
            flash('Расчет завершен. Текущих активов не обнаружено. Все старые записи удалены.', 'info')
            return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))

        all_isins = list(final_holdings_by_isin.keys())
        # Получаем метаданные, ключом является ISIN
        securities_metadata_by_isin = fetch_moex_securities_metadata(all_isins)
        fetched_prices_by_isin = fetch_moex_securities_prices(securities_meta=securities_metadata_by_isin)
        
        InvestmentAsset.query.filter_by(platform_id=platform.id).delete()
        added_count = 0
        for isin, data in final_holdings_by_isin.items():
            metadata = securities_metadata_by_isin.get(isin)
            if metadata and metadata.get('ticker'):
                name, asset_type = metadata.get('name'), metadata.get('asset_type', 'stock')
                price = fetched_prices_by_isin.get(isin, Decimal('0'))
            else:
                name, asset_type, price = isin, 'stock', Decimal('0')
            
            db.session.add(InvestmentAsset(platform_id=platform.id, ticker=isin, name=name, asset_type=asset_type, quantity=data['quantity'], current_price=price, currency_of_price='RUB', source_account_type='Brokerage'))
            added_count += 1
            
        db.session.commit()
        flash(f'Активы успешно рассчитаны по истории сделок. Найдено и обновлено {added_count} позиций.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при расчете активов по сделкам: {e}', 'danger')
    return redirect(url_for('securities.ui_broker_detail', platform_id=platform.id))

@securities_bp.route('/assets/refresh-historical-data', methods=['POST'])
def ui_refresh_securities_historical_data():
    # Импортируем здесь, чтобы избежать циклических зависимостей
    from analytics_logic import refresh_securities_price_change_data
    success, message = refresh_securities_price_change_data()
    if success:
        flash(message, 'success')
    else:
        flash(message, 'danger')
    return redirect(url_for('securities.ui_securities_assets'))

@securities_bp.route('/assets')
def ui_securities_assets():
    # Запрос всех активов с типом 'stock_broker' и количеством больше нуля
    all_securities_assets = InvestmentAsset.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'stock_broker',
        InvestmentAsset.quantity > 0
    ).options(joinedload(InvestmentAsset.platform)).all()

    currency_rates_to_rub = {'RUB': Decimal('1.0'), 'USD': Decimal('90.0'), None: Decimal('1.0')}

    if not all_securities_assets:
        return render_template('securities_assets.html', assets=[], grand_total_rub=0, platform_summary=[])

    # Агрегация активов по ISIN (в нашей модели это поле ticker)
    aggregated_assets = defaultdict(lambda: {
        'total_quantity': Decimal(0),
        'total_value_rub': Decimal(0),
        'locations': [],
        'current_price': Decimal(0),
        'currency_of_price': 'RUB',
        'name': '',
        'asset_type': ''
    })

    platform_summary_agg = defaultdict(lambda: {'total_rub': Decimal(0), 'id': None})
    grand_total_rub = Decimal(0)

    for asset in all_securities_assets:
        isin = asset.ticker
        quantity = asset.quantity or Decimal(0)
        price = asset.current_price or Decimal(0)
        rate = currency_rates_to_rub.get(asset.currency_of_price, Decimal(1.0))
        asset_value_rub = quantity * price * rate

        agg = aggregated_assets[isin]
        agg['total_quantity'] += quantity
        agg['total_value_rub'] += asset_value_rub
        agg['current_price'] = price
        agg['currency_of_price'] = asset.currency_of_price or 'RUB'
        agg['name'] = asset.name
        agg['asset_type'] = asset.asset_type
        agg['locations'].append({'platform_name': asset.platform.name, 'platform_id': asset.platform_id, 'quantity': quantity})
        platform_summary_agg[asset.platform.name]['id'] = asset.platform_id
        platform_summary_agg[asset.platform.name]['total_rub'] += asset_value_rub
        grand_total_rub += asset_value_rub

    all_isins = list(aggregated_assets.keys())
    
    # Получаем данные об изменении цен из кэша
    price_changes = db.session.query(
        HistoricalPriceCache.ticker, 
        HistoricalPriceCache.period, 
        HistoricalPriceCache.change_percent
    ).filter(HistoricalPriceCache.ticker.in_(all_isins)).all()
    
    # Инициализируем словарь с ключами по умолчанию, чтобы избежать ошибок в шаблоне
    changes_by_isin = defaultdict(lambda: {'1d': None, '7d': None, '30d': None, '365d': None})
    for isin, period, change in price_changes:
        if period in changes_by_isin[isin]: # Обновляем только те периоды, которые используются в шаблоне
            changes_by_isin[isin][period] = change

    # Добавляем изменения в агрегированные данные
    for isin, data in aggregated_assets.items():
        data.update(changes_by_isin[isin])

    final_assets_list = sorted(aggregated_assets.items(), key=lambda item: item[1]['total_value_rub'], reverse=True)
    platform_summary = sorted(platform_summary_agg.items(), key=lambda item: item[1]['total_rub'], reverse=True)

    # --- Получаем несколько последних новостей для превью ---
    try:
        latest_news = get_securities_news(limit=5)
    except Exception as e:
        current_app.logger.error(f"Не удалось загрузить превью новостей фондового рынка: {e}")
        latest_news = []

    return render_template('securities_assets.html', assets=final_assets_list, grand_total_rub=grand_total_rub, 
                           platform_summary=platform_summary, latest_news=latest_news)

@securities_bp.route('/transactions')
def ui_securities_transactions():
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort_by', 'timestamp')
    order = request.args.get('order', 'desc')
    filter_platform_id = request.args.get('filter_platform_id', 'all')
    filter_type = request.args.get('filter_type', 'all')

    # Base query for transactions from stock brokers
    query = Transaction.query.join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'stock_broker')
    
    # Eager load platform to avoid N+1 queries
    query = query.options(joinedload(Transaction.platform))

    # Apply filters
    if filter_platform_id != 'all':
        query = query.filter(Transaction.platform_id == int(filter_platform_id))
    if filter_type != 'all':
        query = query.filter(Transaction.type == filter_type)

    # Apply sorting
    sort_column = getattr(Transaction, sort_by, Transaction.timestamp)
    if order == 'desc':
        query = query.order_by(desc(sort_column))
    else:
        query = query.order_by(asc(sort_column))
    
    # Paginate the results
    pagination = query.paginate(page=page, per_page=50, error_out=False)
    
    # Get distinct values for filters
    platforms = InvestmentPlatform.query.filter_by(platform_type='stock_broker').order_by(InvestmentPlatform.name).all()
    unique_transaction_types = [r[0] for r in db.session.query(Transaction.type).join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'stock_broker').distinct().order_by(Transaction.type).all()]

    return render_template('securities_transactions.html', 
                           transactions=pagination.items,
                           pagination=pagination,
                           sort_by=sort_by, order=order, 
                           filter_platform_id=filter_platform_id,
                           filter_type=filter_type,
                           platforms=platforms,
                           unique_transaction_types=unique_transaction_types)