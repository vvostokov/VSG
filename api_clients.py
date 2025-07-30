import hmac
import hashlib
import base64
import json
import time
import requests
from datetime import datetime, timedelta, timezone, date
from urllib.parse import urlencode
from decimal import Decimal

# --- Константы базовых URL API ---
BYBIT_BASE_URL = "https://api.bybit.com"
BITGET_BASE_URL = "https://api.bitget.com"
BINGX_BASE_URL = "https://open-api.bingx.com"
KUCOIN_BASE_URL = "https://api.kucoin.com"
OKX_BASE_URL = "https://www.okx.com"


# --- Вспомогательные функции для аутентификации и запросов ---

def _make_request(method, url, headers=None, params=None, data=None):
    """Универсальная функция для выполнения HTTP-запросов."""
    MAX_RETRIES = 5
    retry_delay_seconds = 5 # Начальная задержка

    for attempt in range(MAX_RETRIES):
        try:
            full_url_with_params = url
            if params:
                full_url_with_params += '?' + urlencode(params)
            print(f"--- [Raw Request Debug] Requesting URL: {full_url_with_params}")
            response = requests.request(method, url, headers=headers, params=params, data=data, timeout=20)
            print(f"--- [Raw Request Debug] Response status for {url}: {response.status_code}")

            if response.status_code == 429:
                print(f"--- [Rate Limit] Получен статус 429 от {url}. Попытка {attempt + 1}/{MAX_RETRIES}. Пауза на {retry_delay_seconds} секунд...")
                time.sleep(retry_delay_seconds)
                retry_delay_seconds *= 2 # Увеличиваем задержку для следующей попытки
                continue

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Ошибка сетевого запроса к {url}: {e}")
            raise Exception(f"Ошибка сети при обращении к API: {e}") from e
        except Exception as e:
            print(f"--- [Raw Request Debug] Unexpected error in _make_request for {url}: {e}")
            raise

    raise Exception(f"Превышено максимальное количество попыток ({MAX_RETRIES}) для запроса к {url} после ошибок с ограничением скорости.")
def _get_timestamp_ms():
    """Возвращает текущее время в миллисекундах."""
    return str(int(time.time() * 1000))
def _convert_bybit_timestamp(timestamp_val):
    """
    Конвертирует timestamp Bybit (ожидается в миллисекундах) в объект datetime.
    Включает эвристическую проверку для очень маленьких значений, которые могут быть в секундах.
    
    """
    try:
        timestamp_raw = int(timestamp_val)
        # Предполагаем миллисекунды, как указано в документации Bybit
        timestamp_in_seconds = timestamp_raw / 1000.0

        # Эвристическая проверка: если дата получается до 2000 года, возможно, это были секунды
        # и timestamp_raw достаточно большой, чтобы быть корректным timestamp в секундах (т.е. > 1 млрд)
        dt_obj = datetime.fromtimestamp(timestamp_in_seconds, tz=timezone.utc)
        if dt_obj.year < 2000 and timestamp_raw > 1000000000:
            print(f"Warning: Bybit timestamp {timestamp_raw} resulted in {dt_obj.year} (before 2000). Retrying as seconds.")
            dt_obj = datetime.fromtimestamp(timestamp_raw, tz=timezone.utc)
            
        return dt_obj
    except (ValueError, TypeError) as e:
        print(f"Error converting Bybit timestamp '{timestamp_val}': {e}. Returning Unix epoch start.")
        return datetime(1970, 1, 1, tzinfo=timezone.utc) # Возвращаем начало эпохи Unix для невалидных timestamp'ов
def _bingx_api_get(api_key: str, api_secret: str, endpoint: str, params: dict = None):
    """Внутренняя функция для выполнения GET-запросов к BingX с подписью."""
    timestamp = _get_timestamp_ms()

    # Строка для подписи формируется из параметров запроса и timestamp.
    sign_params = {'timestamp': timestamp}
    if params:
        sign_params['apiKey'] = api_key # BingX требует apiKey в параметрах для подписи
        sign_params.update(params)

    # Сортируем и кодируем параметры для подписи
    query_string_to_sign = urlencode(sorted(sign_params.items()))

    # Создаем подпись
    signature = hmac.new(api_secret.encode('utf-8'), query_string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    # Финальные параметры для URL-запроса (уже содержат timestamp)
    request_params = sign_params.copy()
    request_params['signature'] = signature

    url = f"{BINGX_BASE_URL}{endpoint}"

    headers = {'X-BX-APIKEY': api_key}
    try:
        response_data = _make_request('GET', url, headers=headers, params=request_params)
        if response_data.get('code') != 0: # BingX использует 0 для успеха
            print(f"Предупреждение API BingX для {endpoint}: {response_data.get('msg')}")
            return None
        return response_data
    except Exception as e:
        print(f"Исключение при запросе к BingX {endpoint}: {e}")
        return None
def _bitget_api_get(api_key: str, api_secret: str, passphrase: str, endpoint: str, params: dict = None):
    """Внутренняя функция для выполнения GET-запросов к Bitget с подписью."""
    timestamp = _get_timestamp_ms()
    method = 'GET'
    
    # Construct the requestPath including query parameters for signing
    request_path = endpoint
    if params:
        # Sort parameters and encode them to form the query string
        sorted_params = sorted(params.items())
        query_string = urlencode(sorted_params)
        request_path += '?' + query_string

    # Bitget signing rule: timestamp + method + requestPath + body (body is empty for GET)
    prehash = timestamp + method + request_path
    signature = base64.b64encode(hmac.new(api_secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
    
    headers = {
        'ACCESS-KEY': api_key,
        'ACCESS-SIGN': signature,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': passphrase,
        'Content-Type': 'application/json' # Good practice
    }
    
    url = f"{BITGET_BASE_URL}{endpoint}"
    try:
        # Pass params separately to _make_request, which will handle URL encoding
        response_data = _make_request(method, url, headers=headers, params=params)
        if response_data.get('code') != '00000':
            print(f"Предупреждение API Bitget для {endpoint}: {response_data.get('msg')}")
            return None
        return response_data
    except Exception as e:
        print(f"Исключение при запросе к Bitget {endpoint}: {e}")
        return None
# --- Функции для получения публичных данных о курсах ---

def fetch_bybit_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с Bybit."""
    print(f"Получение реальных данных с Bybit (прямой API) для символов: {target_symbols}")
    endpoint = "/v5/market/tickers"
    url = f"{BYBIT_BASE_URL}{endpoint}"
    try:
        response_data = _make_request('GET', url, params={'category': 'spot'})
        if response_data.get('retCode') != 0:
            raise Exception(f"Ошибка API Bybit: {response_data.get('retMsg')}")
        
        all_tickers = {item['symbol']: item for item in response_data.get('result', {}).get('list', [])}
        formatted_data = []
        for symbol in target_symbols:
            if symbol in all_tickers:
                ticker = all_tickers[symbol]
                change_24h = float(ticker.get('price24hPcnt', '0')) * 100
                formatted_data.append({
                    'ticker': ticker['symbol'].replace('USDT', ''), # Приводим к короткому имени
                    'price': Decimal(ticker['lastPrice']), # Конвертируем в Decimal
                    'change_pct': change_24h # Используем ключ, ожидаемый в шаблоне
                })
        return formatted_data
    except Exception as e:
        print(f"Ошибка при получении тикеров Bybit: {e}")
        return []

def fetch_bybit_historical_price_range(symbol: str, start_date: date, end_date: date) -> dict[date, Decimal]:
    """
    Получает диапазон исторических цен закрытия для символа с Bybit.
    Возвращает словарь {дата: цена}. 
    ИСПРАВЛЕНО: Добавлена пагинация для запроса данных за периоды > 1000 дней.
    """
    endpoint = "/v5/market/kline"
    prices = {}
    current_start_date = start_date

    while current_start_date <= end_date:
        start_ts_ms = int(datetime.combine(current_start_date, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
        
        params = {
            'category': 'spot',
            'symbol': symbol,
            'interval': 'D', # Дневной интервал
            'start': start_ts_ms,
            'limit': 1000 # Максимальный лимит за один запрос
        }

        try:
            print(f"--- [Bybit History Fetch] Запрос для {symbol} с {current_start_date.isoformat()}...")
            response_data = _make_request('GET', f"{BYBIT_BASE_URL}{endpoint}", params=params)
            
            if response_data.get('retCode') == 0 and response_data.get('result', {}).get('list'):
                kline_list = response_data['result']['list']
                if not kline_list:
                    # Если API вернул пустой список, значит, данных больше нет
                    print(f"--- [Bybit History Fetch] Получен пустой список для {symbol} с {current_start_date.isoformat()}, завершение.")
                    break

                last_kline_date = None
                for kline in kline_list:
                    # kline[0] - timestamp в мс, kline[4] - цена закрытия
                    kline_date = datetime.fromtimestamp(int(kline[0]) / 1000, tz=timezone.utc).date()
                    if start_date <= kline_date <= end_date:
                        prices[kline_date] = Decimal(kline[4])
                    last_kline_date = kline_date
                
                # Перемещаемся на следующий день после последней полученной даты
                if last_kline_date:
                    current_start_date = last_kline_date + timedelta(days=1)
                else:
                    break
                
                time.sleep(0.2) # Пауза между запросами, чтобы не попасть под rate limit
            else:
                print(f"--- [Bybit History Fetch] Ошибка API или нет данных для {symbol} с {current_start_date.isoformat()}. Код: {response_data.get('retCode')}, Сообщение: {response_data.get('retMsg')}")
                break
        except Exception as e:
            print(f"--- [API Error] Не удалось получить историю цен для {symbol} с {current_start_date.isoformat()}: {e}")
            break # Прерываем цикл при ошибке сети

    return prices

def fetch_bitget_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с Bitget."""
    print(f"Получение реальных данных с Bitget (прямой API) для символов: {target_symbols}")
    endpoint = "/api/v2/spot/market/tickers"
    url = f"{BITGET_BASE_URL}{endpoint}"
    try:
        # Bitget public tickers do not require a timestamp parameter.
        # Fetch all tickers and filter locally.
        response_data = _make_request('GET', url)
        if response_data.get('code') != '00000':
            raise Exception(f"Ошибка API Bitget: {response_data.get('msg')}")
        
        all_tickers = {item['symbol']: item for item in response_data.get('data', [])}
        formatted_data = []
        for symbol in target_symbols:
            # Символ в target_symbols уже должен быть в формате API (например, BTCUSDT)
            if symbol in all_tickers:
                ticker_data = all_tickers[symbol]
                change_24h = float(ticker_data.get('priceChangePercent24h', '0')) * 100
                formatted_data.append({
                    'ticker': symbol.replace('USDT', ''), # Очищенный тикер
                    'price': Decimal(ticker_data['lastPr']), # Цена как Decimal
                    'change_pct': change_24h
                })
        return formatted_data
    except Exception as e:
        print(f"Ошибка при получении тикеров Bitget: {e}")
        return []

def fetch_bingx_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с BingX."""
    print(f"Получение реальных данных с BingX (прямой API) для символов: {target_symbols}")
    endpoint = "/openApi/spot/v1/ticker/24hr"
    url = f"{BINGX_BASE_URL}{endpoint}"
    try:
        # ИСПРАВЛЕНО: Этот публичный эндпоинт не требует подписи, но, судя по логам, требует timestamp.
        params = {'timestamp': _get_timestamp_ms()}
        response_data = _make_request('GET', url, params=params)
        if response_data.get('code') != 0:
            raise Exception(f"Ошибка API BingX: {response_data.get('msg')}")
        
        all_tickers = {item['symbol']: item for item in response_data.get('data', [])}
        formatted_data = []
        for symbol in target_symbols:
            if symbol in all_tickers:
                ticker_data = all_tickers[symbol]
                change_24h_str = ticker.get('priceChangePercent', '0')
                # Remove '%' if present, then convert to float
                if isinstance(change_24h_str, str) and change_24h_str.endswith('%'):
                    change_24h = float(change_24h_str.rstrip('%'))
                else:
                    change_24h = float(change_24h_str)
                formatted_data.append({
                    'ticker': symbol.replace('-USDT', ''), # Очищенный тикер
                    'price': Decimal(ticker_data['lastPrice']), # Цена как Decimal
                    'change_pct': change_24h
                })
        return formatted_data
    except Exception as e:
        print(f"Ошибка при получении тикеров BingX: {e}")
        return []

def fetch_kucoin_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с KuCoin."""
    print(f"Получение реальных данных с KuCoin (прямой API) для символов: {target_symbols}")
    endpoint = "/api/v1/market/allTickers"
    url = f"{KUCOIN_BASE_URL}{endpoint}"
    try:
        response_data = _make_request('GET', url)
        if response_data.get('code') != '200000':
            raise Exception(f"Ошибка API KuCoin: {response_data.get('msg')}")
        
        all_tickers = {item['symbol']: item for item in response_data.get('data', {}).get('ticker', [])}
        formatted_data = []
        for symbol in target_symbols:
            if symbol in all_tickers:
                ticker_data = all_tickers[symbol]
                change_24h = float(ticker.get('changeRate', '0')) * 100
                formatted_data.append({
                    'ticker': symbol.replace('-USDT', ''), # Очищенный тикер
                    'price': Decimal(ticker_data['last']), # Цена как Decimal
                    'change_pct': change_24h
                })
        return formatted_data
    except Exception as e:
        print(f"Ошибка при получении тикеров KuCoin: {e}")
        return []

def fetch_okx_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с OKX."""
    print(f"Получение реальных данных с OKX (прямой API) для символов: {target_symbols}")
    endpoint = "/api/v5/market/tickers"
    url = f"{OKX_BASE_URL}{endpoint}"
    try:
        response_data = _make_request('GET', url, params={'instType': 'SPOT'})
        if response_data.get('code') != '0':
            raise Exception(f"Ошибка API OKX: {response_data.get('msg')}")
        
        all_tickers = {item['instId']: item for item in response_data.get('data', [])}
        formatted_data = []
        for symbol in target_symbols:
            if symbol in all_tickers:
                ticker_data = all_tickers[symbol]
                change_24h = float(ticker.get('chg24h', '0')) * 100
                formatted_data.append({
                    'ticker': symbol.replace('-USDT', ''), # Очищенный тикер
                    'price': Decimal(ticker_data['last']), # Цена как Decimal
                    'change_pct': change_24h
                })
        return formatted_data
    except Exception as e:
        print(f"Ошибка при получении тикеров OKX: {e}")
        return []

def fetch_bingx_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с BingX."""
    print(f"Получение реальных балансов с BingX (прямой API) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret: # BingX не использует passphrase для спотового API
        raise Exception("Для BingX необходимы API ключ и секрет.")

    assets_map = {}

    # 1. Получаем баланс Spot Account
    # Примечание: API BingX v1 предоставляет только один эндпоинт для балансов, который, по-видимому,
    # объединяет средства со спотового и основного (Funding) счетов.
    print("\n--- [BingX] Попытка получить баланс Spot Account ---")
    spot_data = _bingx_api_get(api_key, api_secret, '/openApi/spot/v1/account/balance')
    if spot_data and spot_data.get('code') == 0 and spot_data.get('data', {}).get('balances'):
        for asset_data in spot_data['data']['balances']:
            quantity = float(asset_data.get('free', 0)) + float(asset_data.get('locked', 0))
            if quantity > 1e-9:
                key = (asset_data['asset'], 'Spot')
                assets_map[key] = assets_map.get(key, 0.0) + quantity
    else:
        print(f"[BingX Debug] Raw spot_data response: {json.dumps(spot_data, indent=2) if spot_data else 'No response'}")
        print("[BingX] Не удалось получить баланс Spot Account или он пуст.")
    
    # Примечание: Получение балансов Funding и Earn для BingX отключено.
    # API не предоставляет отдельных эндпоинтов для этих кошельков.
    # Эндпоинт для Earn (/openApi/wealth/v1/savings/position) и Funding
    # возвращает ошибку "api is not exist". Это может быть связано с отсутствием
    # необходимых прав у API-ключа ("Wealth") или с тем, что эндпоинт устарел.
    print("\n--- [BingX] Получение балансов Funding и Earn пропущено (API не предоставляет эндпоинты). ---")


    all_assets = []
    for (ticker, account_type), quantity in assets_map.items():
        all_assets.append({'ticker': ticker, 'quantity': str(quantity), 'account_type': account_type})
    return all_assets


# --- Функции для получения балансов аккаунтов (требуют аутентификации) ---

def fetch_bybit_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с Bybit, включая Funding и Earn."""
    print(f"Получение реальных балансов с Bybit (прямой API, включая Funding и Earn) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для Bybit необходимы API ключ и секрет.")

    def _bybit_api_get(path, params):
        """Внутренняя функция для выполнения GET-запросов к Bybit."""
        timestamp = _get_timestamp_ms()
        recv_window = "20000"
        
        params_with_recv_window = params.copy()
        params_with_recv_window['recvWindow'] = recv_window
        query_string = urlencode(dict(sorted(params_with_recv_window.items())))

        payload = f"{timestamp}{api_key}{recv_window}{query_string}"
        signature = hmac.new(api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

        url = f"{BYBIT_BASE_URL}{path}?{query_string}"
        headers = {
            'X-BAPI-API-KEY': api_key,
            'X-BAPI-TIMESTAMP': timestamp,
            'X-BAPI-RECV-WINDOW': recv_window,
            'X-BAPI-SIGN': signature,
            'Content-Type': 'application/json'
        }
        print(f"\n--- [Bybit] Запрос к: {path} с параметрами {params} ---")
        return _make_request('GET', url, headers=headers)

    assets_map = {} # Используем словарь для удобного обновления и избежания дубликатов

    # 1. Получаем баланс Unified Trading Account
    print("\n--- [Bybit] Попытка получить баланс Unified Trading Account ---")
    try:
        unified_data = _bybit_api_get('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
        if unified_data.get('retCode') == 0:
            print("[Bybit] Запрос к Unified Trading Account успешен.")
            if unified_data.get('result', {}).get('list'):
                for coin_balance in unified_data['result']['list'][0].get('coin', []):
                    # ИСПРАВЛЕНО: Возвращаемся к простому получению общего баланса.
                    # Ваше уточнение подтвердило, что заблокированные средства в Unified-аккаунте — это открытые ордера, а не Earn-активы.
                    # Поэтому мы прекращаем попытки автоматического разделения и берем общий баланс 'walletBalance'.
                    balance = float(coin_balance.get('walletBalance', 0))
                    if balance > 0:
                        key = (coin_balance['coin'], 'Unified Trading')
                        assets_map[key] = assets_map.get(key, 0.0) + balance
            else:
                print("[Bybit] Баланс Unified Trading Account пуст.")
        else:
            print(f"[Bybit] Ошибка API для Unified Trading Account: {unified_data.get('retMsg')}")
    except Exception as e:
        print(f"Исключение при получении баланса Unified Account: {e}")

    # 2. Получаем баланс Funding Account
    print("\n--- [Bybit] Попытка получить баланс Funding Account ---")
    try:
        funding_data = _bybit_api_get('/v5/asset/transfer/query-account-coins-balance', {'accountType': 'FUND'})
        print(f"[Bybit] Ответ от Funding Account: {json.dumps(funding_data, indent=2)}")
        if funding_data.get('retCode') == 0:
            print("[Bybit] Запрос к Funding Account успешен.")
            if funding_data.get('result', {}).get('balance'):
                for coin_balance in funding_data['result']['balance']:
                    balance = float(coin_balance.get('walletBalance', 0))
                    if balance > 0:
                        key = (coin_balance['coin'], 'Funding')
                        assets_map[key] = assets_map.get(key, 0.0) + balance
            else:
                print("[Bybit] Баланс Funding Account пуст.")
        else:
            print(f"[Bybit] Ошибка API для Funding Account: {funding_data.get('retMsg')}")
    except Exception as e:
        print(f"Исключение при получении баланса Funding Account: {e}")

    # 3. Получаем баланс Earn, перебирая известные категории продуктов
    # Этот эндпоинт требует прав на чтение для "Earn" в ключе API.
    print("\n--- [Bybit] Попытка получить баланс Earn (Flexible Savings, On-Chain) ---")
    
    # Определяем категории продуктов Earn для перебора
    earn_categories = ['FlexibleSaving', 'OnChain'] # Документированные категории
    
    for category in earn_categories:
        try:
            print(f"\n--- [Bybit] Запрос позиций Earn для категории: {category} ---")
            # ИСПРАВЛЕНО: Запросы без параметров или только с 'coin' возвращали ошибку.
            # Пробуем запрашивать позиции, указывая обязательный параметр 'category'.
            earn_data = _bybit_api_get('/v5/earn/position', {'category': category})
            print(f"[Bybit] Ответ от Earn Account (категория: {category}): {json.dumps(earn_data, indent=2)}")
            
            if earn_data.get('retCode') == 0:
                print(f"[Bybit] Запрос к Earn Account (категория: {category}) успешен.")
                if earn_data.get('result', {}).get('list'):
                    for earn_position in earn_data['result']['list']:
                        # ИСПРАВЛЕНО: Согласно вашим логам, правильное поле для суммы - 'amount', а не 'totalPrincipalAmount'.
                        principal = float(earn_position.get('amount', 0))
                        if principal > 1e-9:
                            coin = earn_position['coin']
                            key = (coin, 'Earn')
                            assets_map[key] = assets_map.get(key, 0.0) + principal
                            print(f"[Bybit] {coin}: найдено {principal} в Earn (категория: {category}).")
            # Не считаем ошибкой, если API вернул код, отличный от 0, т.к. у пользователя может не быть продуктов в данной категории.
            elif earn_data.get('retCode') != 0:
                print(f"[Bybit] Информация: не удалось получить баланс Earn для категории {category}: {earn_data.get('retMsg')}.")
        except Exception as e:
            print(f"Исключение при получении баланса Earn для категории {category}: {e}")

    # Формируем итоговый список из словаря, отфильтровывая нулевые балансы
    all_assets = []
    for (ticker, account_type), quantity in assets_map.items():
        if quantity > 1e-9: # Используем небольшой порог для сравнения с нулем
            all_assets.append({'ticker': ticker, 'quantity': str(quantity), 'account_type': account_type})

    return all_assets
def fetch_bybit_deposit_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list:
    """Получает всю историю депозитов (зачислений) с Bybit."""
    print(f"Получение истории депозитов с Bybit с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для Bybit необходимы API ключ и секрет.")

    def _bybit_api_get(path, params):
        """Внутренняя функция для выполнения GET-запросов к Bybit."""
        timestamp = _get_timestamp_ms()
        recv_window = "20000"
        params_with_recv_window = params.copy()
        params_with_recv_window['recvWindow'] = recv_window
        query_string = urlencode(dict(sorted(params_with_recv_window.items())))
        payload = f"{timestamp}{api_key}{recv_window}{query_string}"
        signature = hmac.new(api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        url = f"{BYBIT_BASE_URL}{path}?{query_string}"
        headers = {'X-BAPI-API-KEY': api_key, 'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': recv_window, 'X-BAPI-SIGN': signature}
        return _make_request('GET', url, headers=headers)

    all_deposits = []
    end_time = end_time_dt if end_time_dt else datetime.now(timezone.utc)
    if start_time_dt:
        if start_time_dt.tzinfo is None:
            limit_date = start_time_dt.replace(tzinfo=timezone.utc)
        else:
            limit_date = start_time_dt.astimezone(timezone.utc)
    else:
        limit_date = end_time - timedelta(days=2*365)
    history_limit_reached = False
    while end_time > limit_date:
        start_time = end_time - timedelta(days=7) # API позволяет запрашивать до 30 дней
        start_ts_ms = int(start_time.timestamp() * 1000)
        end_ts_ms = int(end_time.timestamp() * 1000)
        
        print(f"--- [Bybit Deposits] Запрос за период: {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}")

        cursor = ""
        while True:
            params = {'limit': 50, 'startTime': start_ts_ms, 'endTime': end_ts_ms}
            if cursor:
                params['cursor'] = cursor
            response_data = _bybit_api_get('/v5/asset/deposit/query-record', params)
            ret_code = response_data.get('retCode')
            if ret_code == 10001: # 'Can't query earlier than 2 years'
                print(f"--- [Bybit Deposits] Достигнут предел истории в 2 года. Завершение сбора данных.")
                history_limit_reached = True
                break
            elif ret_code != 0:
                raise Exception(f"Ошибка API Bybit при получении истории депозитов: {response_data.get('retMsg')}")
            result = response_data.get('result', {})
            deposits = result.get('rows', [])

            if deposits:
                all_deposits.extend(deposits)
            cursor = result.get('nextPageCursor')
            if not cursor:
                break
        if history_limit_reached:
            break
        end_time = start_time
        time.sleep(0.2)

    unique_deposits = list({d['txID']: d for d in all_deposits}.values())
    print(f"--- [Bybit Deposits] Всего найдено {len(all_deposits)} депозитов, уникальных: {len(unique_deposits)}.")
    return unique_deposits
def fetch_bybit_internal_deposit_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list:
    """Получает всю историю внутренних депозитов (зачислений от других пользователей Bybit)."""
    print(f"Получение истории внутренних депозитов с Bybit с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для Bybit необходимы API ключ и секрет.")

    def _bybit_api_get(path, params):
        """Внутренняя функция для выполнения GET-запросов к Bybit."""
        timestamp = _get_timestamp_ms()
        recv_window = "20000"
        params_with_recv_window = params.copy()
        params_with_recv_window['recvWindow'] = recv_window
        query_string = urlencode(dict(sorted(params_with_recv_window.items())))
        payload = f"{timestamp}{api_key}{recv_window}{query_string}"
        signature = hmac.new(api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        url = f"{BYBIT_BASE_URL}{path}?{query_string}"
        headers = {'X-BAPI-API-KEY': api_key, 'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': recv_window, 'X-BAPI-SIGN': signature}
        return _make_request('GET', url, headers=headers)

    all_deposits = []
    end_time = end_time_dt if end_time_dt else datetime.now(timezone.utc)
    if start_time_dt:
        if start_time_dt.tzinfo is None:
            limit_date = start_time_dt.replace(tzinfo=timezone.utc)
        else:
            limit_date = start_time_dt.astimezone(timezone.utc)
    else:
        limit_date = end_time - timedelta(days=2*365)
    history_limit_reached = False
    while end_time > limit_date:
        start_time = end_time - timedelta(days=7) # API позволяет запрашивать до 30 дней
        start_ts_ms = int(start_time.timestamp() * 1000)
        end_ts_ms = int(end_time.timestamp() * 1000)
        
        print(f"--- [Bybit Internal Deposits] Запрос за период: {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}")

        cursor = ""
        while True:
            params = {'limit': 50, 'startTime': start_ts_ms, 'endTime': end_ts_ms}
            if cursor:
                params['cursor'] = cursor
            response_data = _bybit_api_get('/v5/asset/deposit/query-internal-record', params)
            ret_code = response_data.get('retCode')
            if ret_code == 10001: # 'Can't query earlier than 2 years'
                print(f"--- [Bybit Internal Deposits] Достигнут предел истории в 2 года. Завершение сбора данных.")
                history_limit_reached = True
                break
            elif ret_code != 0:
                raise Exception(f"Ошибка API Bybit при получении истории внутренних депозитов: {response_data.get('retMsg')}")
            result = response_data.get('result', {})
            deposits = result.get('rows', [])
            if deposits:
                all_deposits.extend(deposits)
            cursor = result.get('nextPageCursor')
            if not cursor:
                break
        if history_limit_reached:
            break
        end_time = start_time
        time.sleep(0.2)

    unique_deposits = list({d['id']: d for d in all_deposits}.values())
    print(f"--- [Bybit Internal Deposits] Всего найдено {len(all_deposits)} внутренних депозитов, уникальных: {len(unique_deposits)}.")
    return unique_deposits

def fetch_bybit_trade_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list: # Renamed from fetch_bybit_withdrawal_history
    """Получает всю историю спотовых сделок (покупок/продаж) с Bybit."""
    print(f"Получение истории спотовых сделок с Bybit с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для Bybit необходимы API ключ и секрет.")

    def _bybit_api_get(path, params):
        """Внутренняя функция для выполнения GET-запросов к Bybit."""
        timestamp = _get_timestamp_ms()
        recv_window = "20000"
        params_with_recv_window = params.copy()
        params_with_recv_window['recvWindow'] = recv_window
        query_string = urlencode(dict(sorted(params_with_recv_window.items())))
        payload = f"{timestamp}{api_key}{recv_window}{query_string}"
        signature = hmac.new(api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        url = f"{BYBIT_BASE_URL}{path}?{query_string}"
        headers = {'X-BAPI-API-KEY': api_key, 'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': recv_window, 'X-BAPI-SIGN': signature}
        return _make_request('GET', url, headers=headers)

    all_trades = []
    end_time = end_time_dt if end_time_dt else datetime.now(timezone.utc)
    if start_time_dt:
        if start_time_dt.tzinfo is None:
            limit_date = start_time_dt.replace(tzinfo=timezone.utc)
        else:
            limit_date = start_time_dt.astimezone(timezone.utc)
    else:
        limit_date = end_time - timedelta(days=2*365)
    history_limit_reached = False
    while end_time > limit_date:
        start_time = end_time - timedelta(days=7) # API позволяет запрашивать до 30 дней
        start_ts_ms = int(start_time.timestamp() * 1000)
        end_ts_ms = int(end_time.timestamp() * 1000)
        
        print(f"--- [Bybit Trades] Запрос за период: {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}")

        cursor = ""
        while True:
            params = {
                'category': 'spot',
                'limit': 50,
                'startTime': start_ts_ms,
                'endTime': end_ts_ms,
                'orderStatus': 'Filled' # Только исполненные ордера
            }
            if cursor:
                params['cursor'] = cursor
            
            response_data = _bybit_api_get('/v5/execution/list', params)
            print(f"--- [Bybit Trades Debug] Raw API response for {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}: {json.dumps(response_data, indent=2)}")
            ret_code = response_data.get('retCode')
            if ret_code == 10001: # 'Can't query order earlier than 2 years'
                print(f"--- [Bybit Trades] Достигнут предел истории в 2 года. Завершение сбора данных.")
                history_limit_reached = True
                break
            elif ret_code != 0:
                print(f"--- [Bybit Trades Debug] API Error: retCode={ret_code}, retMsg={response_data.get('retMsg')}")
                raise Exception(f"Ошибка API Bybit при получении истории сделок: {response_data.get('retMsg')}")
            
            result = response_data.get('result', {})
            trades = result.get('list', []) # Спотовая история сделок находится в 'list'

            if trades:
                all_trades.extend(trades)
            cursor = result.get('nextPageCursor')
            if not cursor:
                break
        if history_limit_reached:
            break
        end_time = start_time
        time.sleep(0.2)

    # Используем execId как уникальный идентификатор для сделок
    unique_trades = list({t['execId']: t for t in all_trades}.values())
    print(f"--- [Bybit Trades] Всего найдено {len(all_trades)} сделок, уникальных: {len(unique_trades)}.")
    return unique_trades
def fetch_bybit_withdrawal_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list:
    """Получает всю историю выводов средств с Bybit."""
    print(f"Получение истории выводов с Bybit с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для Bybit необходимы API ключ и секрет.")

    def _bybit_api_get(path, params):
        """Внутренняя функция для выполнения GET-запросов к Bybit."""
        timestamp = _get_timestamp_ms()
        recv_window = "20000"
        params_with_recv_window = params.copy()
        params_with_recv_window['recvWindow'] = recv_window
        query_string = urlencode(dict(sorted(params_with_recv_window.items())))
        payload = f"{timestamp}{api_key}{recv_window}{query_string}"
        signature = hmac.new(api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        url = f"{BYBIT_BASE_URL}{path}?{query_string}"
        headers = {'X-BAPI-API-KEY': api_key, 'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': recv_window, 'X-BAPI-SIGN': signature}
        return _make_request('GET', url, headers=headers)

    all_withdrawals = []
    end_time = end_time_dt if end_time_dt else datetime.now(timezone.utc)
    if start_time_dt:
        if start_time_dt.tzinfo is None:
            limit_date = start_time_dt.replace(tzinfo=timezone.utc)
        else:
            limit_date = start_time_dt.astimezone(timezone.utc)
    else:
        limit_date = end_time - timedelta(days=2*365)
    history_limit_reached = False
    while end_time > limit_date:
        start_time = end_time - timedelta(days=7) # API позволяет запрашивать до 30 дней
        start_ts_ms = int(start_time.timestamp() * 1000)
        end_ts_ms = int(end_time.timestamp() * 1000)
        
        print(f"--- [Bybit Withdrawals] Запрос за период: {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}")

        cursor = ""
        while True:
            params = {'limit': 50, 'startTime': start_ts_ms, 'endTime': end_ts_ms}
            if cursor:
                params['cursor'] = cursor
            response_data = _bybit_api_get('/v5/asset/withdraw/query-record', params)
            try:
                print(f"--- [Bybit Withdrawals Debug] Raw API response for {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}: {json.dumps(response_data, indent=2)}") # Keep this for debugging
            except TypeError as e:
                print(f"--- [Bybit Withdrawals Debug] Could not JSON dump response for {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}: {response_data} (Error: {e})")
           
            ret_code = response_data.get('retCode')
            if ret_code == 10001: # 'Can't query earlier than 2 years'
                print(f"--- [Bybit Withdrawals] Достигнут предел истории в 2 года. Завершение сбора данных.")
                history_limit_reached = True
                break
            elif ret_code != 0:
                print(f"--- [Bybit Withdrawals Debug] API Error: retCode={ret_code}, retMsg={response_data.get('retMsg')}")
                raise Exception(f"Ошибка API Bybit при получении истории выводов: {response_data.get('retMsg')}")
            
            result = response_data.get('result', {})
            withdrawals = result.get('rows', []) # ИСПРАВЛЕНО: Выводы находятся в 'rows'
            
            if withdrawals:
                all_withdrawals.extend(withdrawals)
            cursor = result.get('nextPageCursor')
            if not cursor:
                break
        if history_limit_reached:
            break
        end_time = start_time
        time.sleep(0.2)

    unique_withdrawals = list({w['txID']: w for w in all_withdrawals}.values())
    print(f"--- [Bybit Withdrawals] Всего найдено {len(all_withdrawals)} выводов, уникальных: {len(unique_withdrawals)}.")
    return unique_withdrawals

def fetch_bybit_transfer_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list:
    """Получает историю внутренних переводов с Bybit."""
    print(f"Получение истории переводов с Bybit с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для Bybit необходимы API ключ и секрет.")

    def _bybit_api_get(path, params):
        """Внутренняя функция для выполнения GET-запросов к Bybit (повторное определение для ясности)."""
        timestamp = _get_timestamp_ms()
        recv_window = "20000"
        params_with_recv_window = params.copy()
        params_with_recv_window['recvWindow'] = recv_window
        query_string = urlencode(dict(sorted(params_with_recv_window.items())))
        payload = f"{timestamp}{api_key}{recv_window}{query_string}"
        signature = hmac.new(api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
        url = f"{BYBIT_BASE_URL}{path}?{query_string}"
        headers = {'X-BAPI-API-KEY': api_key, 'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': recv_window, 'X-BAPI-SIGN': signature}
        return _make_request('GET', url, headers=headers)

    all_transfers = []
    end_time = end_time_dt if end_time_dt else datetime.now(timezone.utc)
    if start_time_dt:
        if start_time_dt.tzinfo is None:
            limit_date = start_time_dt.replace(tzinfo=timezone.utc)
        else:
            limit_date = start_time_dt.astimezone(timezone.utc)
    else:
        limit_date = end_time - timedelta(days=2*365)

    history_limit_reached = False
    while end_time > limit_date:
        start_time = end_time - timedelta(days=7)
        start_ts_ms = int(start_time.timestamp() * 1000)
        end_ts_ms = int(end_time.timestamp() * 1000)
        
        print(f"--- [Bybit History] Запрос за период: {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}")

        cursor = ""
        while True: # Внутренний цикл для пагинации внутри 30-дневного окна
            params = {'limit': 50, 'startTime': start_ts_ms, 'endTime': end_ts_ms}
            if cursor:
                params['cursor'] = cursor

            response_data = _bybit_api_get('/v5/asset/transfer/query-inter-transfer-list', params)
            ret_code = response_data.get('retCode')
            if ret_code == 10001: # 'Can't query earlier than 2 years'
                print(f"--- [Bybit Transfers] Достигнут предел истории в 2 года. Завершение сбора данных.")
                history_limit_reached = True
                break
            elif ret_code != 0:
                raise Exception(f"Ошибка API Bybit при получении истории переводов: {response_data.get('retMsg')}")

            result = response_data.get('result', {})
            transfers = result.get('list', [])
            if transfers:
                all_transfers.extend(transfers)

            cursor = result.get('nextPageCursor')
            if not cursor:
                break # Пагинация для этого 30-дневного периода завершена

        if history_limit_reached:
            break

        end_time = start_time # Переходим к предыдущему 7-дневному периоду
        time.sleep(0.2)

    # Удаляем дубликаты на случай пересечения временных рамок или особенностей API
    unique_transfers = list({t['transferId']: t for t in all_transfers}.values())
    print(f"--- [Bybit History] Всего найдено {len(all_transfers)} транзакций, уникальных: {len(unique_transfers)}.")
    return unique_transfers
def fetch_bybit_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> dict:
    """
    Агрегатор для получения всех типов транзакций с Bybit (переводы, депозиты).
    Возвращает словарь, где ключи - типы транзакций.
    """
    all_txs = {
        'transfers': [],
        'deposits': [], # Внешние депозиты (on-chain)
        'internal_deposits': [], # Внутренние депозиты (от других пользователей Bybit)
        'withdrawals': [], # Выводы средств
        'trades': [] # Новое поле для сделок
    }
    try:
        all_txs['transfers'] = fetch_bybit_transfer_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt)
    except Exception as e:
        print(f"Не удалось получить историю переводов Bybit: {e}")
    try:
        all_txs['deposits'] = fetch_bybit_deposit_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt)
    except Exception as e:
        print(f"Не удалось получить историю депозитов Bybit: {e}")
    try:
        all_txs['internal_deposits'] = fetch_bybit_internal_deposit_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt)
    except Exception as e:
        print(f"--- [ERROR] Failed to fetch Bybit withdrawal history: {e}") # More prominent error
        print(f"Не удалось получить историю внутренних депозитов Bybit: {e}")
    try:
        all_txs['withdrawals'] = fetch_bybit_withdrawal_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt) # Correctly call and assign
    except Exception as e:
        print(f"--- [ERROR] Failed to fetch Bybit withdrawal history: {e}")
        print(f"Не удалось получить историю выводов Bybit: {e}")
    try:
        all_txs['trades'] = fetch_bybit_trade_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt) # Вызываем новую функцию
    except Exception as e:
        print(f"--- [ERROR] Failed to fetch Bybit trade history: {e}")
        print(f"Не удалось получить историю сделок Bybit: {e}")
 
    return all_txs
def fetch_bitget_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с Bitget, включая Spot и Earn."""
    print(f"Получение реальных балансов с Bitget (прямой API, включая Spot и Earn) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для Bitget необходимы API ключ, секрет и парольная фраза.")


    assets_map = {}

    # 1. Получаем баланс Spot Account
    print("\n--- [Bitget] Попытка получить баланс Spot Account ---")
    spot_data = _bitget_api_get(api_key, api_secret, passphrase, '/api/v2/spot/account/assets')
    if spot_data:
        for asset_data in spot_data.get('data', []):
            quantity = float(asset_data.get('available', 0)) + float(asset_data.get('frozen', 0))
            if quantity > 1e-9:
                key = (asset_data['coin'], 'Spot')
                assets_map[key] = assets_map.get(key, 0.0) + quantity

    # 2. Получаем баланс Earn Account
    print("\n--- [Bitget] Попытка получить баланс Earn Account ---")
    earn_data = _bitget_api_get(api_key, api_secret, passphrase, '/api/v2/earn/account/assets')
    if earn_data:
        for asset_data in earn_data.get('data', []):
            quantity = float(asset_data.get('amount', 0))
            if quantity > 1e-9:
                key = (asset_data['coin'], 'Earn')
                assets_map[key] = assets_map.get(key, 0.0) + quantity

    all_assets = []
    for (ticker, account_type), quantity in assets_map.items():
        all_assets.append({'ticker': ticker, 'quantity': str(quantity), 'account_type': account_type})
    return all_assets
def fetch_bitget_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> dict:
    """
    Агрегатор для получения всех типов транзакций с Bitget (депозиты, выводы, сделки).
    """
    print(f"Получение истории транзакций с Bitget с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для Bitget необходимы API ключ, секрет и парольная фраза.")

    start_ts_ms = int(start_time_dt.timestamp() * 1000) if start_time_dt else None
    end_ts_ms = int(end_time_dt.timestamp() * 1000) if end_time_dt else None

    def _fetch_paginated_data_with_time(endpoint, id_key_for_record, pagination_param_name, base_params=None):
        """Общая функция для получения данных с пагинацией Bitget."""
        all_records = []
        last_id = None
        
        current_params = base_params.copy() if base_params else {}
        if start_ts_ms: current_params['startTime'] = start_ts_ms
        if end_ts_ms: current_params['endTime'] = end_ts_ms

        while True:
            current_params['limit'] = 100

            if last_id:
                current_params[pagination_param_name] = last_id
            
            response_data = _bitget_api_get(api_key, api_secret, passphrase, endpoint, current_params)
            if not response_data or not response_data.get('data'):
                break
            
            records = response_data['data']
            all_records.extend(records)
            
            if len(records) < 100:
                break
            
            last_id = records[-1][id_key_for_record]
            time.sleep(0.2)
        return all_records

    all_txs = {
        'deposits': [],
        'withdrawals': [],
        'trades': []
    }
    
    # --- Deposits ---
    try:
        print("\n--- [Bitget] Получение истории депозитов ---")
        all_txs['deposits'] = _fetch_paginated_data_with_time('/api/v2/asset/deposit-record', 'id', 'idLessThan')
    except Exception as e:
        print(f"Не удалось получить историю депозитов Bitget: {e}")
        
    # --- Withdrawals ---
    try:
        print("\n--- [Bitget] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_paginated_data_with_time('/api/v2/asset/withdrawal-record', 'withdrawId', 'idLessThan')
    except Exception as e:
        print(f"Не удалось получить историю выводов Bitget: {e}")
        
    # --- Trades ---
    try:
        print("\n--- [Bitget] Получение истории сделок ---")
        # УЛУЧШЕНО: Запрашиваем сделки для всех пар, не ограничиваясь текущими активами или парами к USDT.
        # Это позволяет корректно находить историю по активам, которые были полностью проданы.
        # Фильтруем по времени вручную, так как startTime/endTime не работают надежно для этого эндпоинта.
        all_trades = []
        last_id = None
        stop_fetching = False
        while not stop_fetching:
            # Запрашиваем без указания symbol, чтобы получить сделки по всем парам
            params = {'limit': 500}
            if last_id:
                params['after'] = last_id
            
            response_data = _bitget_api_get(api_key, api_secret, passphrase, '/api/v2/spot/trade/fills', params)
            if not response_data or not response_data.get('data'):
                break
            
            trades_page = response_data['data']
            for trade in trades_page:
                trade_ts_ms = int(trade.get('cTime', 0))
                # Если мы дошли до сделок старше нашего начального времени, прекращаем.
                if start_ts_ms and trade_ts_ms < start_ts_ms:
                    stop_fetching = True
                    break
                # Добавляем сделку, если она попадает в наш временной диапазон
                if (not start_ts_ms or trade_ts_ms >= start_ts_ms) and (not end_ts_ms or trade_ts_ms <= end_ts_ms):
                    all_trades.append(trade)

            if stop_fetching or len(trades_page) < 500:
                break

            last_id = trades_page[-1]['tradeId']
            time.sleep(0.2)
            
        all_txs['trades'] = all_trades
    except Exception as e:
        print(f"Не удалось получить историю сделок Bitget: {e}")
    
    print(f"--- [Bitget History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок.")
    return all_txs

def fetch_bingx_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> dict:
    """
    Агрегатор для получения всех типов транзакций с BingX (депозиты, выводы, сделки).
    """
    print(f"Получение истории транзакций с BingX с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для BingX необходимы API ключ и секрет.")

    start_ts_ms = int(start_time_dt.timestamp() * 1000) if start_time_dt else None
    end_ts_ms = int(end_time_dt.timestamp() * 1000) if end_time_dt else None

    def _fetch_bingx_paginated_data(endpoint, time_key='time', start_time=None, end_time=None):
        """Общая функция для получения данных с пагинацией BingX по времени."""
        all_records = []
        current_end_time = end_time if end_time is not None else int(time.time() * 1000)
        current_start_time = start_time if start_time is not None else current_end_time - (2 * 365 * 24 * 60 * 60 * 1000) # По умолчанию 2 года

        # BingX API позволяет запрашивать до 7 дней для некоторых эндпоинтов истории.
        chunk_size_ms = 7 * 24 * 60 * 60 * 1000 # 7 дней в миллисекундах

        while current_end_time > current_start_time:
            temp_start_time = max(current_start_time, current_end_time - chunk_size_ms)
            
            print(f"--- [BingX History] Запрос за период: {datetime.fromtimestamp(temp_start_time / 1000, tz=timezone.utc).strftime('%Y-%m-%d')} -> {datetime.fromtimestamp(current_end_time / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}")

            params = {
                'startTime': temp_start_time,
                'endTime': current_end_time,
                'limit': 100 # Максимальный лимит
            }
            
            response_data = _bingx_api_get(api_key, api_secret, endpoint, params)
            if not response_data or not response_data.get('data'):
                current_end_time = temp_start_time - 1 # Переходим к предыдущему чанку
                continue
            
            records = response_data['data']
            all_records.extend(records)
            
            current_end_time = temp_start_time - 1 # Переходим к предыдущему чанку
            time.sleep(0.2) # Ограничение скорости запросов

        return all_records

    all_txs = {'deposits': [], 'withdrawals': [], 'trades': []}
    try:
        print("\n--- [BingX] Получение истории депозитов ---")
        all_txs['deposits'] = _fetch_bingx_paginated_data('/openApi/spot/v1/wallet/deposit/history', 'createTime', start_time=start_ts_ms, end_time=end_ts_ms)
    except Exception as e:
        print(f"Не удалось получить историю депозитов BingX: {e}")
    try:
        print("\n--- [BingX] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_bingx_paginated_data('/openApi/spot/v1/wallet/withdraw/history', 'createTime', start_time=start_ts_ms, end_time=end_ts_ms)
    except Exception as e:
        print(f"Не удалось получить историю выводов BingX: {e}")
    try:
        print("\n--- [BingX] Получение истории сделок ---")
        all_txs['trades'] = _fetch_bingx_paginated_data('/openApi/spot/v1/trade/myTrades', 'time', start_time=start_ts_ms, end_time=end_ts_ms)
    except Exception as e:
        print(f"Не удалось получить историю сделок BingX: {e}")
    
    print(f"--- [BingX History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок.")
    return all_txs

def fetch_okx_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с OKX, включая Trading, Funding и Financial (Earn)."""
    print(f"Получение реальных балансов с OKX (прямой API) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для OKX необходимы API ключ, секрет и парольная фраза.")

    def _okx_api_get(endpoint, params=None):
        """Внутренняя функция для выполнения GET-запросов к OKX."""
        timestamp = datetime.utcnow().isoformat()[:-3] + 'Z'
        method = 'GET'
        
        query_string = f"?{urlencode(params)}" if params else ""
        request_path = f"{endpoint}{query_string}"
        
        prehash = timestamp + method + request_path
        signature = base64.b64encode(hmac.new(api_secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
        
        headers = {
            'OK-ACCESS-KEY': api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': passphrase,
            'Content-Type': 'application/json'
        }
        
        url = f"{OKX_BASE_URL}{endpoint}"
        try:
            response_data = _make_request(method, url, headers=headers, params=params)
            if response_data.get('code') != '0':
                print(f"Предупреждение API OKX для {endpoint}: {response_data.get('msg')}")
                return None
            return response_data
        except Exception as e:
            print(f"Исключение при запросе к OKX {endpoint}: {e}")
            return None

    assets_map = {}

    # 1. Получаем баланс Trading Account (Unified)
    print("\n--- [OKX] Попытка получить баланс Trading Account ---")
    trading_data = _okx_api_get('/api/v5/account/balance')
    if trading_data and trading_data.get('data'):
        for asset_data in trading_data['data'][0].get('details', []):
            quantity = float(asset_data.get('cashBal', 0))
            if quantity > 1e-9:
                assets_map[(asset_data['ccy'], 'Trading')] = assets_map.get((asset_data['ccy'], 'Trading'), 0.0) + quantity

    # 2. Получаем баланс Funding Account
    print("\n--- [OKX] Попытка получить баланс Funding Account ---")
    funding_data = _okx_api_get('/api/v5/asset/balances')
    if funding_data and funding_data.get('data'):
        for asset_data in funding_data['data']:
            quantity = float(asset_data.get('bal', 0))
            if quantity > 1e-9:
                assets_map[(asset_data['ccy'], 'Funding')] = assets_map.get((asset_data['ccy'], 'Funding'), 0.0) + quantity

    # 3. Получаем баланс Financial Account (Earn/Savings)
    print("\n--- [OKX] Попытка получить баланс Financial Account (Earn) ---")
    # ИСПРАВЛЕНО: Эндпоинт /api/v5/asset/financial-balance возвращал 404. Используем правильный эндпоинт /api/v5/finance/savings/balance.
    financial_data = _okx_api_get('/api/v5/finance/savings/balance')
    if financial_data and financial_data.get('data'):
        for asset_data in financial_data['data']:
            quantity = float(asset_data.get('amt', 0)) # ИСПРАВЛЕНО: Используем ключ 'amt' вместо 'bal' для получения суммы.
            if quantity > 1e-9:
                assets_map[(asset_data['ccy'], 'Earn')] = assets_map.get((asset_data['ccy'], 'Earn'), 0.0) + quantity

    all_assets = []
    for (ticker, account_type), quantity in assets_map.items():
        all_assets.append({'ticker': ticker, 'quantity': str(quantity), 'account_type': account_type})
    return all_assets

def fetch_okx_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> dict:
    """
    Агрегатор для получения всех типов транзакций с OKX (депозиты, выводы, сделки).
    """
    print(f"Получение истории транзакций с OKX с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для OKX необходимы API ключ, секрет и парольная фраза.")

    start_ts_ms = int(start_time_dt.timestamp() * 1000) if start_time_dt else None
    end_ts_ms = int(end_time_dt.timestamp() * 1000) if end_time_dt else None

    def _okx_api_get(endpoint, params=None):
        """Внутренняя функция для выполнения GET-запросов к OKX."""
        timestamp = datetime.utcnow().isoformat()[:-3] + 'Z'
        method = 'GET'
        
        query_string = f"?{urlencode(params)}" if params else ""
        request_path = f"{endpoint}{query_string}"
        
        prehash = timestamp + method + request_path
        signature = base64.b64encode(hmac.new(api_secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
        
        headers = {
            'OK-ACCESS-KEY': api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': passphrase,
            'Content-Type': 'application/json'
        }
        
        url = f"{OKX_BASE_URL}{endpoint}"
        try:
            response_data = _make_request(method, url, headers=headers, params=params)
            if response_data.get('code') != '0':
                print(f"Предупреждение API OKX для {endpoint}: {response_data.get('msg')}")
                return None
            return response_data
        except Exception as e:
            print(f"Исключение при запросе к OKX {endpoint}: {e}")
            return None

    def _fetch_okx_paginated_data(endpoint, id_key, params=None):
        """Общая функция для получения данных с пагинацией OKX."""
        all_records = []
        last_id = None
        if params is None:
            params = {}
        
        # Add time parameters only for endpoints that support them
        if endpoint in ['/api/v5/asset/deposit-history', '/api/v5/asset/withdrawal-history']:
            if start_ts_ms: params['begin'] = start_ts_ms
            if end_ts_ms: params['end'] = end_ts_ms

        while True:
            if last_id:
                params['after'] = last_id
            
            response_data = _okx_api_get(endpoint, params)
            if not response_data or not response_data.get('data'):
                break
            
            records = response_data['data']
            all_records.extend(records)
            
            if len(records) < 100: # OKX возвращает до 100 записей на страницу
                break
            
            last_id = records[-1][id_key]
            time.sleep(0.2) # Ограничение скорости запросов
        return all_records

    all_txs = {'deposits': [], 'withdrawals': [], 'trades': []}
    try:
        print("\n--- [OKX] Получение истории депозитов ---")
        all_txs['deposits'] = _fetch_okx_paginated_data('/api/v5/asset/deposit-history', 'depId')
    except Exception as e:
        print(f"Не удалось получить историю депозитов OKX: {e}")
    try:
        print("\n--- [OKX] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_okx_paginated_data('/api/v5/asset/withdrawal-history', 'wdId')
    except Exception as e:
        print(f"Не удалось получить историю выводов OKX: {e}")
    try:
        print("\n--- [OKX] Получение истории сделок ---")
        # ИСПРАВЛЕНО: Используем правильный эндпоинт для истории сделок OKX.
        # Временные параметры не поддерживаются этим эндпоинтом, поэтому _fetch_okx_paginated_data их не добавит.
        # Фильтрация по времени будет выполнена позже, если необходимо.
        all_trades_raw = _fetch_okx_paginated_data('/api/v5/trade/fills-history', 'tradeId', params={'instType': 'SPOT'})
        
        # Фильтруем вручную, так как API возвращает только за последние 3 месяца
        all_txs['trades'] = [
            t for t in all_trades_raw 
            if (not start_ts_ms or int(t.get('ts', 0)) >= start_ts_ms) and (not end_ts_ms or int(t.get('ts', 0)) <= end_ts_ms)
        ]
    except Exception as e:
        print(f"Не удалось получить историю сделок OKX: {e}")
    
    print(f"--- [OKX History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок.")
    return all_txs

def _kucoin_api_get(api_key: str, api_secret: str, passphrase: str, endpoint: str, params: dict = None):
    """Внутренняя функция для выполнения GET-запросов к KuCoin с подписью."""
    timestamp = _get_timestamp_ms()
    method = 'GET'
    
    query_string = f"?{urlencode(params)}" if params else ""
    request_path = f"{endpoint}{query_string}"
    
    prehash = timestamp + method + request_path
    signature = base64.b64encode(hmac.new(api_secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
    
    # ИСПРАВЛЕНО: KuCoin требует, чтобы парольная фраза была подписана с помощью HMAC-SHA256, используя API Secret в качестве ключа,
    # а затем результат был закодирован в Base64.
    passphrase_signature = hmac.new(api_secret.encode('utf-8'), passphrase.encode('utf-8'), hashlib.sha256)
    encrypted_passphrase = base64.b64encode(passphrase_signature.digest()).decode('utf-8')
    
    headers = {
        'KC-API-KEY': api_key,
        'KC-API-SIGN': signature,
        'KC-API-TIMESTAMP': timestamp,
        'KC-API-PASSPHRASE': encrypted_passphrase,
        'KC-API-KEY-VERSION': '2',
        'Content-Type': 'application/json'
    }
    
    url = f"{KUCOIN_BASE_URL}{endpoint}"
    try:
        response_data = _make_request(method, url, headers=headers, params=params)
        if response_data.get('code') != '200000':
            print(f"Предупреждение API KuCoin для {endpoint}: {response_data.get('msg')}")
            return None
        return response_data
    except Exception as e:
        print(f"Исключение при запросе к KuCoin {endpoint}: {e}")
        return None

def fetch_kucoin_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с KuCoin, включая Main, Trade и Earn."""
    print(f"Получение реальных балансов с KuCoin (прямой API) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для KuCoin необходимы API ключ, секрет и парольная фраза.")

    assets_map = {}
    
    # KuCoin API возвращает все счета одним запросом
    print("\n--- [KuCoin] Попытка получить балансы со всех счетов ---")
    all_accounts_data = _kucoin_api_get(api_key, api_secret, passphrase, '/api/v1/accounts')
    if all_accounts_data and all_accounts_data.get('data'):
        for account in all_accounts_data['data']:
            quantity = float(account.get('balance', 0))
            if quantity > 1e-9:
                account_type_raw = account.get('type', 'unknown')
                # Маппинг типов счетов KuCoin в наши типы
                account_type_map = {
                    'main': 'Funding',
                    'trade': 'Trading',
                    'earn': 'Earn',
                    'margin': 'Margin'
                }
                account_type = account_type_map.get(account_type_raw, account_type_raw.capitalize())
                
                key = (account['currency'], account_type)
                assets_map[key] = assets_map.get(key, 0.0) + quantity

    all_assets = []
    for (ticker, account_type), quantity in assets_map.items():
        all_assets.append({'ticker': ticker, 'quantity': str(quantity), 'account_type': account_type})
    return all_assets

def fetch_kucoin_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> dict:
    """
    Агрегатор для получения всех типов транзакций с KuCoin (депозиты, выводы, сделки).
    """
    print(f"Получение истории транзакций с KuCoin с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для KuCoin необходимы API ключ, секрет и парольная фраза.")

    start_ts_ms = int(start_time_dt.timestamp() * 1000) if start_time_dt else None
    end_ts_ms = int(end_time_dt.timestamp() * 1000) if end_time_dt else None

    def _fetch_kucoin_paginated_data(endpoint, params=None):
        """Общая функция для получения данных с пагинацией KuCoin."""
        all_records = []
        current_page = 1
        if params is None:
            params = {}
        
        while True:
            params['currentPage'] = current_page
            params['pageSize'] = 500 # Максимальный размер страницы для KuCoin
            if start_ts_ms: params['startAt'] = start_ts_ms
            if end_ts_ms: params['endAt'] = end_ts_ms
            
            response_data = _kucoin_api_get(api_key, api_secret, passphrase, endpoint, params)
            if not response_data or not response_data.get('data', {}).get('items'):
                break
            
            records = response_data['data']['items']
            all_records.extend(records)
            
            total_num = response_data['data'].get('totalNum', 0)
            if len(all_records) >= total_num:
                break
            
            current_page += 1
            time.sleep(0.3) # Ограничение скорости запросов
        return all_records

    all_txs = {'deposits': [], 'withdrawals': [], 'trades': []}
    try:
        print("\n--- [KuCoin] Получение истории депозитов ---")
        all_txs['deposits'] = _fetch_kucoin_paginated_data('/api/v1/deposits')
    except Exception as e:
        print(f"Не удалось получить историю депозитов KuCoin: {e}")
    try:
        print("\n--- [KuCoin] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_kucoin_paginated_data('/api/v1/withdrawals')
    except Exception as e:
        print(f"Не удалось получить историю выводов KuCoin: {e}")
    try:
        print("\n--- [KuCoin] Получение истории сделок ---")
        all_txs['trades'] = _fetch_kucoin_paginated_data('/api/v1/fills')
    except Exception as e:
        print(f"Не удалось получить историю сделок KuCoin: {e}")
    
    print(f"--- [KuCoin History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок.")
    return all_txs

# Примечание: Функции для получения балансов с BingX, KuCoin, OKX не реализованы,
# так как они не были добавлены в SYNC_DISPATCHER в app.py.
# Их можно реализовать по аналогии с Bybit и Bitget при необходимости.

# --- API Dispatchers ---
# Maps a platform name (lowercase) to the function that syncs its assets.
SYNC_DISPATCHER = {
    'bybit': fetch_bybit_account_assets,
    'bitget': fetch_bitget_account_assets,
    'bingx': fetch_bingx_account_assets,
    'kucoin': fetch_kucoin_account_assets,
    'okx': fetch_okx_account_assets,
}

# Maps a platform name to the function that syncs its transaction history.
SYNC_TRANSACTIONS_DISPATCHER = {
    'bybit': fetch_bybit_all_transactions,
    'bitget': fetch_bitget_all_transactions,
    'bingx': fetch_bingx_all_transactions,
    'okx': fetch_okx_all_transactions,
    'kucoin': fetch_kucoin_all_transactions,
}

# Maps a platform name to the function that fetches its market prices.
PRICE_TICKER_DISPATCHER = {
    'bybit': {'func': fetch_bybit_spot_tickers, 'suffix': 'USDT'},
    'bitget': {'func': fetch_bitget_spot_tickers, 'suffix': 'USDT'},
    'bingx': {'func': fetch_bingx_spot_tickers, 'suffix': '-USDT'},
    'kucoin': {'func': fetch_kucoin_spot_tickers, 'suffix': '-USDT'},
    'okx': {'func': fetch_okx_spot_tickers, 'suffix': '-USDT'},
}
