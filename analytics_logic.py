import time
import json
from datetime import date, timedelta, datetime, timezone
from collections import defaultdict, namedtuple
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from sqlalchemy import func

from extensions import db
from models import (
    Transaction, InvestmentPlatform, SecuritiesPortfolioHistory, InvestmentAsset, HistoricalPriceCache, PortfolioHistory,
    JsonCache
)
from securities_logic import fetch_moex_historical_prices
from api_clients import fetch_bybit_historical_price_range, fetch_bybit_spot_tickers, PRICE_TICKER_DISPATCHER

def refresh_securities_portfolio_history():
    """
    Пересчитывает и сохраняет ежедневную стоимость портфеля ценных бумаг.
    """
    print("--- [Analytics] Начало обновления истории портфеля ЦБ ---")
    
    first_tx = Transaction.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'stock_broker'
    ).order_by(Transaction.timestamp.asc()).first()

    if not first_tx:
        print("--- [Analytics] Нет транзакций по ЦБ, обновление истории отменено.")
        return False, "Нет транзакций для расчета истории."

    start_date = first_tx.timestamp.date()
    end_date = date.today()
    
    all_txs = Transaction.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'stock_broker'
    ).order_by(Transaction.timestamp.asc()).all()

    SecuritiesPortfolioHistory.query.delete()
    db.session.commit()

    holdings = defaultdict(Decimal)
    tx_index = 0
    
    for current_date in pd.date_range(start=start_date, end=end_date):
        current_date = current_date.date()
        
        while tx_index < len(all_txs) and all_txs[tx_index].timestamp.date() <= current_date:
            tx = all_txs[tx_index]
            if tx.asset1_ticker:
                amount = tx.asset1_amount if tx.type == 'buy' else -tx.asset1_amount
                holdings[tx.asset1_ticker] += amount
            tx_index += 1
        
        current_holdings = {isin: qty for isin, qty in holdings.items() if qty > 0}
        
        if not current_holdings:
            db.session.add(SecuritiesPortfolioHistory(date=current_date, total_value_rub=Decimal(0)))
            continue
            
        prices = fetch_moex_historical_prices(list(current_holdings.keys()), current_date)
        
        total_value = sum(quantity * prices.get(isin, Decimal(0)) for isin, quantity in current_holdings.items())
            
        db.session.add(SecuritiesPortfolioHistory(date=current_date, total_value_rub=total_value))

    db.session.commit()
    print(f"--- [Analytics] История портфеля ЦБ обновлена с {start_date} по {end_date}. ---")
    return True, "История портфеля ценных бумаг успешно обновлена."

def refresh_crypto_portfolio_history():
    """
    Пересчитывает и сохраняет ежедневную стоимость крипто-портфеля. Оптимизированная версия.
    """
    print("--- [Analytics] Начало обновления истории крипто-портфеля (оптимизированная версия) ---")
    
    first_tx = Transaction.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'crypto_exchange'
    ).order_by(Transaction.timestamp.asc()).first()

    if not first_tx:
        print("--- [Analytics] Нет транзакций по крипто, обновление истории отменено.")
        return False, "Нет транзакций для расчета истории."

    start_date = first_tx.timestamp.date()
    end_date = date.today()
    
    all_txs = Transaction.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'crypto_exchange'
    ).order_by(Transaction.timestamp.asc()).all()

    # 1. Определяем все уникальные тикеры за всю историю
    all_tickers = set()
    for tx in all_txs:
        if tx.asset1_ticker: all_tickers.add(tx.asset1_ticker)
        if tx.asset2_ticker: all_tickers.add(tx.asset2_ticker)
    
    stablecoins = {'USDT', 'USDC', 'DAI'}
    tickers_to_fetch = [t for t in all_tickers if t not in stablecoins]
    print(f"--- [Analytics] Будут запрошены исторические цены для: {tickers_to_fetch}")

    # 2. Загружаем всю историю цен для каждого тикера одним пакетным запросом
    historical_prices_cache = defaultdict(dict)
    for ticker in tickers_to_fetch:
        symbol = f"{ticker}USDT"
        print(f"--- [Analytics] Загрузка истории для {symbol}...")
        prices = fetch_bybit_historical_price_range(symbol, start_date, end_date)
        historical_prices_cache[ticker] = prices
        time.sleep(0.2) # Небольшая задержка между запросами по тикерам

    # 3. Проходим по дням и считаем портфель, используя кэш цен
    PortfolioHistory.query.delete()
    db.session.commit()

    holdings = defaultdict(Decimal)
    tx_index = 0
    currency_rates_to_rub = {'USDT': Decimal('90.0')}

    for current_date_dt in pd.date_range(start=start_date, end=end_date):
        current_date = current_date_dt.date()
        
        while tx_index < len(all_txs) and all_txs[tx_index].timestamp.date() <= current_date:
            tx = all_txs[tx_index]
            # Логика обновления холдингов
            if tx.type == 'buy':
                if tx.asset1_ticker: holdings[tx.asset1_ticker] += tx.asset1_amount
                if tx.asset2_ticker: holdings[tx.asset2_ticker] -= tx.asset2_amount
            elif tx.type == 'sell':
                if tx.asset1_ticker: holdings[tx.asset1_ticker] -= tx.asset1_amount
                if tx.asset2_ticker: holdings[tx.asset2_ticker] += tx.asset2_amount
            elif tx.type in ['deposit', 'transfer']: # Учитываем и переводы
                if tx.asset1_ticker: holdings[tx.asset1_ticker] += tx.asset1_amount
            elif tx.type == 'withdrawal':
                if tx.asset1_ticker: holdings[tx.asset1_ticker] -= tx.asset1_amount
            tx_index += 1
        
        current_holdings = {ticker: qty for ticker, qty in holdings.items() if qty > 0.000001}
        
        total_value_usdt = Decimal(0)
        for stable in stablecoins:
            if stable in current_holdings:
                total_value_usdt += current_holdings[stable]

        for ticker, quantity in current_holdings.items():
            if ticker in stablecoins:
                continue

            price_usdt = None
            for i in range(7): # Искать цену за последнюю неделю, если на дату нет торгов
                check_date = current_date - timedelta(days=i)
                if check_date in historical_prices_cache[ticker]:
                    price_usdt = historical_prices_cache[ticker][check_date]
                    break
            
            if price_usdt is not None:
                total_value_usdt += quantity * price_usdt
            else:
                print(f"--- [Analytics Warning] Не найдена историческая цена для {ticker} на {current_date} или ранее.")
        
        total_value_rub = total_value_usdt * currency_rates_to_rub.get('USDT', Decimal(1.0))
        db.session.add(PortfolioHistory(date=current_date, total_value_rub=total_value_rub))

    db.session.commit()
    print(f"--- [Analytics] История крипто-портфеля обновлена с {start_date} по {end_date}. ---")
    return True, "История крипто-портфеля успешно обновлена."

def refresh_securities_price_change_data():
    """
    Обновляет кэш с изменениями цен для всех ценных бумаг.
    """
    print("--- [Analytics] Начало обновления кэша изменений цен MOEX ---")
    
    all_isins = [r[0] for r in db.session.query(InvestmentAsset.ticker).join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'stock_broker', InvestmentAsset.quantity > 0).distinct().all()]
    if not all_isins:
        return False, "Нет ценных бумаг для обновления."

    today = date.today()
    periods = {'1d': 1, '7d': 7, '30d': 30, '90d': 90, '180d': 180, '365d': 365}
    
    # Кэшируем запросы, чтобы не запрашивать одну и ту же дату несколько раз
    price_cache_by_date = {}

    for isin in all_isins:
        if today not in price_cache_by_date:
            price_cache_by_date[today] = fetch_moex_historical_prices(all_isins, today)
        today_price = price_cache_by_date[today].get(isin)
        
        if not today_price: continue

        for period_name, days_ago in periods.items():
            past_date = today - timedelta(days=days_ago)
            if past_date not in price_cache_by_date:
                # Запрашиваем цены для всех ISIN на эту дату, чтобы кэшировать
                price_cache_by_date[past_date] = fetch_moex_historical_prices(all_isins, past_date)
            
            past_price = price_cache_by_date[past_date].get(isin)
            change_pct = float(((today_price - past_price) / past_price) * 100) if past_price and past_price > 0 else None
            cache_entry = HistoricalPriceCache.query.filter_by(ticker=isin, period=period_name).first()
            if cache_entry: 
                cache_entry.change_percent = change_pct
                cache_entry.last_updated = datetime.now(timezone.utc)
            else: 
                db.session.add(HistoricalPriceCache(ticker=isin, period=period_name, change_percent=change_pct))
            
    db.session.commit()
    return True, f"Кэш изменений цен для {len(all_isins)} активов MOEX обновлен."

def refresh_crypto_price_change_data():
    """
    Обновляет кэш с изменениями цен для всех криптоактивов. Оптимизированная версия.
    """
    print("--- [Analytics] Начало обновления кэша изменений цен Crypto (оптимизированная версия) ---")
    
    all_tickers = [r[0] for r in db.session.query(InvestmentAsset.ticker).join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'crypto_exchange', InvestmentAsset.quantity > 0).distinct().all()]
    if not all_tickers:
        return False, "Нет криптоактивов для обновления."

    today = date.today()
    start_date_fetch = today - timedelta(days=366)
    periods = {'24h': 1, '7d': 7, '30d': 30, '90d': 90, '180d': 180, '365d': 365}
    
    historical_prices_cache = defaultdict(dict)
    for ticker in all_tickers:
        if ticker.upper() in ['USDT', 'USDC', 'DAI']: continue
        symbol_usdt = f"{ticker}USDT"
        prices = fetch_bybit_historical_price_range(symbol_usdt, start_date_fetch, today)
        historical_prices_cache[ticker] = prices
        time.sleep(0.2)

    for ticker in all_tickers:
        asset = db.session.query(InvestmentAsset.current_price).filter(InvestmentAsset.ticker == ticker, InvestmentAsset.quantity > 0).order_by(InvestmentAsset.current_price.desc()).first()
        if not asset or not asset.current_price: continue
        
        today_price = asset.current_price

        for period_name, days_ago in periods.items():
            past_date = today - timedelta(days=days_ago)
            past_price = None
            for i in range(7):
                check_date = past_date - timedelta(days=i)
                if check_date in historical_prices_cache.get(ticker, {}):
                    past_price = historical_prices_cache[ticker][check_date]
                    break

            change_pct = float(((today_price - past_price) / past_price) * 100) if past_price and past_price > 0 else None
            cache_entry = HistoricalPriceCache.query.filter_by(ticker=ticker, period=period_name).first()
            if cache_entry:
                cache_entry.change_percent = change_pct
                cache_entry.last_updated = datetime.now(timezone.utc)
            else:
                db.session.add(HistoricalPriceCache(ticker=ticker, period=period_name, change_percent=change_pct))
            
    db.session.commit()
    return True, f"Кэш изменений цен для {len(all_tickers)} криптоактивов обновлен."

def _generate_performance_chart_data(tickers: list[str]) -> dict:
    """
    (Внутренняя функция) Собирает и обрабатывает исторические данные для списка 
    крипто-тикеров для отображения нормализованной производительности за последние три года.
    Данные нормализуются к максимальной цене соответствующего годового периода.
    Оптимизировано с использованием параллельных запросов.
    """
    chart_data = {}
    today = date.today()
    start_date_3y_ago = today - timedelta(days=365 * 3)

    # --- Оптимизация 1: Получаем текущие цены одним запросом ---
    print("--- [Performance Chart] Fetching live prices for chart...")
    symbols_for_api = [f"{ticker}USDT" for ticker in tickers]
    current_prices_data = fetch_bybit_spot_tickers(symbols_for_api)
    current_prices = {item['ticker']: item['price'] for item in current_prices_data}

    # --- Оптимизация 2: Получаем исторические данные параллельно ---
    def fetch_history_for_ticker(ticker):
        """Вспомогательная функция для выполнения в отдельном потоке."""
        symbol = f"{ticker}USDT"
        prices = fetch_bybit_historical_price_range(symbol, start_date_3y_ago, today)
        if ticker in current_prices:
            prices[today] = current_prices[ticker]
        return ticker, prices

    with ThreadPoolExecutor(max_workers=len(tickers) or 1) as executor:
        future_to_ticker = {executor.submit(fetch_history_for_ticker, ticker): ticker for ticker in tickers}
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                _ticker, prices_by_date = future.result()
                if not prices_by_date:
                    print(f"--- [Performance Chart] No data for {ticker}")
                    continue

                # --- Этап 3: Обработка и нормализация данных для каждого тикера ---
                periods = {'0-365': {}, '365-730': {}, '730-1095': {}}
                date_1y_ago = today - timedelta(days=365)
                date_2y_ago = today - timedelta(days=365 * 2)

                for d, price in prices_by_date.items():
                    if d > date_1y_ago: periods['0-365'][d] = price
                    elif d > date_2y_ago: periods['365-730'][d] = price
                    else: periods['730-1095'][d] = price

                ticker_performance = {"labels": list(range(1, 366))}

                def normalize_period(prices: dict, base_date: date) -> list:
                    normalized_data = []
                    if not prices: return [None] * 365
                    max_price = max(prices.values()) if prices else Decimal(0)
                    if max_price > 0:
                        for i in range(364, -1, -1):
                            check_date = base_date - timedelta(days=i)
                            price_found = next((prices.get(check_date - timedelta(days=j)) for j in range(7) if (check_date - timedelta(days=j)) in prices), None)
                            if price_found:
                                normalized_data.append(float(price_found / max_price * 100))
                            else:
                                normalized_data.append(None)
                    else:
                        normalized_data = [0.0] * 365
                    return normalized_data

                ticker_performance['0-365'] = normalize_period(periods['0-365'], today)
                ticker_performance['365-730'] = normalize_period(periods['365-730'], date_1y_ago)
                ticker_performance['730-1095'] = normalize_period(periods['730-1095'], date_2y_ago)

                chart_data[ticker] = ticker_performance

            except Exception as exc:
                print(f'--- [Performance Chart] Ticker {ticker} сгенерировал исключение: {exc}')

    return chart_data

def refresh_performance_chart_data():
    """
    Обновляет данные для графика производительности и сохраняет их в кэш.
    """
    print("--- [Analytics] Начало обновления данных для графика производительности ---")
    try:
        performance_tickers = ['BTC', 'ETH', 'SOL', 'TON', 'SUI', 'NEAR', 'XRP']
        chart_data = _generate_performance_chart_data(performance_tickers)

        cache_key = 'performance_chart_data'
        cache_entry = JsonCache.query.filter_by(cache_key=cache_key).first()
        if not cache_entry:
            cache_entry = JsonCache(cache_key=cache_key)
            db.session.add(cache_entry)
        
        # Используем default=str для сериализации Decimal в строку
        cache_entry.json_data = json.dumps(chart_data, default=str)
        cache_entry.last_updated = datetime.now(timezone.utc)
        db.session.commit()
        print("--- [Analytics] Данные для графика производительности успешно обновлены и сохранены в кэш. ---")
        return True, "Данные для графика производительности успешно обновлены."
    except Exception as e:
        db.session.rollback()
        print(f"--- [Analytics ERROR] Ошибка при обновлении данных для графика производительности: {e}")
        return False, f"Ошибка при обновлении данных: {e}"

def get_performance_chart_data_from_cache():
    """
    Получает данные для графика производительности из кэша.
    Возвращает (data, last_updated_timestamp).
    Если кэш пуст, возвращает пустые данные.
    """
    cache_key = 'performance_chart_data'
    cache_entry = JsonCache.query.filter_by(cache_key=cache_key).first()
    if cache_entry:
        return json.loads(cache_entry.json_data), cache_entry.last_updated
    else:
        return {}, None

def refresh_market_leaders_cache():
    """Fetches and caches market leader data from MOEX and crypto exchanges."""
    print("--- [Analytics] Начало обновления кэша лидеров рынка ---")
    try:
        moex_leaders = fetch_moex_market_leaders(['IMOEX', 'SBER', 'GAZP', 'LKOH', 'ROSN', 'YNDX'])
        crypto_leaders = fetch_bybit_spot_tickers(['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'TONUSDT'])

        market_data = {
            'moex': moex_leaders,
            'crypto': crypto_leaders,
            'last_updated': datetime.now(timezone.utc).isoformat()
        }

        cache_key = 'market_leaders_data'
        cache_entry = JsonCache.query.filter_by(cache_key=cache_key).first()
        if not cache_entry:
            cache_entry = JsonCache(cache_key=cache_key)
            db.session.add(cache_entry)
        
        cache_entry.json_data = json.dumps(market_data, default=str) # Use default=str for Decimal
        db.session.commit()
        print("--- [Analytics] Кэш лидеров рынка успешно обновлен. ---")
        return True, "Кэш лидеров рынка обновлен."
    except Exception as e:
        db.session.rollback()
        print(f"--- [Analytics ERROR] Ошибка при обновлении кэша лидеров рынка: {e}")
        return False, f"Ошибка при обновлении кэша лидеров рынка: {e}"