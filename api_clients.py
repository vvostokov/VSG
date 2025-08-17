import hmac
import hashlib
import base64
import json
import time
import requests
from datetime import datetime, timedelta, timezone, date # noqa
from urllib.parse import urlencode
from decimal import Decimal

# --- Константы базовых URL API ---
BYBIT_BASE_URL = "https://api.bybit.com"
BITGET_BASE_URL = "https://api.bitget.com"
BINGX_BASE_URL = "https://open-api.bingx.com"
KUCOIN_BASE_URL = "https://api.kucoin.com"
OKX_BASE_URL = "https://www.okx.com"

from flask import current_app

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
            current_app.logger.debug(f"--- [Raw Request Debug] Requesting URL: {full_url_with_params}")
            response = requests.request(method, url, headers=headers, params=params, data=data, timeout=20)
            current_app.logger.debug(f"--- [Raw Request Debug] Response status for {url}: {response.status_code}")

            if response.status_code == 429:
                current_app.logger.warning(f"--- [Rate Limit] Получен статус 429 от {url}. Попытка {attempt + 1}/{MAX_RETRIES}. Пауза на {retry_delay_seconds} секунд...")
                time.sleep(retry_delay_seconds)
                retry_delay_seconds *= 2 # Увеличиваем задержку для следующей попытки
                continue

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            current_app.logger.error(f"Ошибка сетевого запроса к {url}: {e}")
            raise Exception(f"Ошибка сети при обращении к API: {e}") from e
        except Exception as e:
            current_app.logger.error(f"--- [Raw Request Debug] Unexpected error in _make_request for {url}: {e}")
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
            current_app.logger.warning(f"Warning: Bybit timestamp {timestamp_raw} resulted in {dt_obj.year} (before 2000). Retrying as seconds.")
            dt_obj = datetime.fromtimestamp(timestamp_raw, tz=timezone.utc)
            
        return dt_obj
    except (ValueError, TypeError) as e:
        current_app.logger.error(f"Error converting Bybit timestamp '{timestamp_val}': {e}. Returning Unix epoch start.")
        return datetime(1970, 1, 1, tzinfo=timezone.utc) # Возвращаем начало эпохи Unix для невалидных timestamp'ов
def _bingx_api_get(api_key: str, api_secret: str, endpoint: str, params: dict = None):
    """Внутренняя функция для выполнения GET-запросов к BingX с подписью."""
    timestamp = _get_timestamp_ms()

    # Строка для подписи формируется из параметров запроса и timestamp.
    # ИСПРАВЛЕНО: apiKey не должен быть частью подписываемой строки согласно документации BingX.
    # Он передается только в заголовке X-BX-APIKEY.
    sign_params = params.copy() if params else {}
    sign_params['timestamp'] = timestamp

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
            current_app.logger.warning(f"Предупреждение API BingX для {endpoint}: {response_data.get('msg')}")
            return None
        return response_data
    except Exception as e:
        current_app.logger.error(f"Исключение при запросе к BingX {endpoint}: {e}")
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
    
    url = f"{BITGET_BASE_URL}{request_path}"
    try:
        response_data = _make_request(method, url, headers=headers)
        if response_data.get('code') != '00000':
            current_app.logger.warning(f"Предупреждение API Bitget для {endpoint}: {response_data.get('msg')}")
            return None
        return response_data
    except Exception as e:
        current_app.logger.error(f"Исключение при запросе к Bitget {endpoint}: {e}")
        return None
# --- Функции для получения публичных данных о курсах ---

def fetch_bybit_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с Bybit."""
    current_app.logger.info(f"Получение реальных данных с Bybit (прямой API) для символов: {target_symbols}")
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
        current_app.logger.error(f"Ошибка при получении тикеров Bybit: {e}")
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
            current_app.logger.info(f"--- [Bybit History Fetch] Запрос для {symbol} с {current_start_date.isoformat()}...")
            response_data = _make_request('GET', f"{BYBIT_BASE_URL}{endpoint}", params=params)
            
            if response_data.get('retCode') == 0 and response_data.get('result', {}).get('list'):
                kline_list = response_data['result']['list']
                if not kline_list:
                    # Если API вернул пустой список, значит, данных больше нет
                    current_app.logger.info(f"--- [Bybit History Fetch] Получен пустой список для {symbol} с {current_start_date.isoformat()}, завершение.")
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
                current_app.logger.warning(f"--- [Bybit History Fetch] Ошибка API или нет данных для {symbol} с {current_start_date.isoformat()}. Код: {response_data.get('retCode')}, Сообщение: {response_data.get('retMsg')}")
                break
        except Exception as e:
            current_app.logger.error(f"--- [API Error] Не удалось получить историю цен для {symbol} с {current_start_date.isoformat()}: {e}")
            break # Прерываем цикл при ошибке сети

    return prices

def fetch_bitget_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с Bitget."""
    current_app.logger.info(f"Получение реальных данных с Bitget (прямой API) для символов: {target_symbols}")
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
        current_app.logger.error(f"Ошибка при получении тикеров Bitget: {e}")
        return []

def fetch_bingx_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с BingX."""
    current_app.logger.info(f"Получение реальных данных с BingX (прямой API) для символов: {target_symbols}")
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
                change_24h_str = ticker_data.get('priceChangePercent', '0')
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
        current_app.logger.error(f"Ошибка при получении тикеров BingX: {e}")
        return []

def fetch_kucoin_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с KuCoin."""
    current_app.logger.info(f"Получение реальных данных с KuCoin (прямой API) для символов: {target_symbols}")
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
                change_24h = float(ticker_data.get('changeRate', '0')) * 100
                formatted_data.append({
                    'ticker': symbol.replace('-USDT', ''), # Очищенный тикер
                    'price': Decimal(ticker_data['last']), # Цена как Decimal
                    'change_pct': change_24h
                })
        return formatted_data
    except Exception as e:
        current_app.logger.error(f"Ошибка при получении тикеров KuCoin: {e}")
        return []

def fetch_okx_spot_tickers(target_symbols: list) -> list:
    """Получает данные о курсах с OKX."""
    current_app.logger.info(f"Получение реальных данных с OKX (прямой API) для символов: {target_symbols}")
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
                change_24h = float(ticker_data.get('chg24h', '0')) * 100
                formatted_data.append({
                    'ticker': symbol.replace('-USDT', ''), # Очищенный тикер
                    'price': Decimal(ticker_data['last']), # Цена как Decimal
                    'change_pct': change_24h
                })
        return formatted_data
    except Exception as e:
        current_app.logger.error(f"Ошибка при получении тикеров OKX: {e}")
        return []

def fetch_cryptocompare_news(limit: int = 50, categories: str = None) -> list:
    """Получает последние новости из CryptoCompare API."""
    api_key = current_app.config.get('CRYPTOCOMPARE_API_KEY')
    if not api_key:
        current_app.logger.warning("CRYPTOCOMPARE_API_KEY не установлен. Запрос новостей не будет выполнен.")
        return []

    # ИЗМЕНЕНО: Убираем feeds из URL и добавляем в параметры.
    # Добавляем 'sentiment': 'true' для явного запроса тональности.
    url = "https://min-api.cryptocompare.com/data/v2/news/"
    params = {
        'lang': 'EN', # Запрашиваем новости на английском, так как 'RU' не поддерживается
        'feeds': 'cryptocompare,cointelegraph,coindesk', # Запрашиваем фиды, где есть тональность
        'sentiment': 'true', # Явно запрашиваем тональность
        'excludeCategories': 'Sponsored', # Исключаем спонсорские посты
        'api_key': api_key
    }
    try:
        if categories:
            params['categories'] = categories
            current_app.logger.info(f"--- [CryptoCompare] Запрос новостей для категорий: {categories}...")
        # Логируем параметры без API ключа для безопасности
        log_params = {k: v for k, v in params.items() if k != 'api_key'}
        current_app.logger.info(f"--- [CryptoCompare] Запрос новостей с параметрами: {log_params}")
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        response_data = response.json()
        if response_data.get('Type') == 100: # 100 is success for CryptoCompare
            # Для отладки, проверим первую новость на наличие поля sentiment
            news_data = response_data.get('Data', [])
            if news_data:
                first_article = news_data[0]
                if 'sentiment' in first_article:
                    current_app.logger.info("--- [CryptoCompare] Поле 'sentiment' присутствует в ответе API.")
                else:
                    current_app.logger.warning("--- [CryptoCompare] ВНИМАНИЕ: Поле 'sentiment' отсутствует в ответе API. Возможно, эта функция не включена для вашего API ключа.")
            return news_data[:limit] # Ограничиваем количество уже после получения
        else:
            current_app.logger.error(f"Ошибка API CryptoCompare: {response_data.get('Message')}")
            return []
    except Exception as e:
        current_app.logger.error(f"Исключение при запросе новостей из CryptoCompare: {e}")
        return []

def fetch_bingx_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с BingX."""
    current_app.logger.info(f"Получение реальных балансов с BingX (прямой API) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret: # BingX не использует passphrase для спотового API
        raise Exception("Для BingX необходимы API ключ и секрет.")

    assets_map = {}

    # 1. Получаем баланс Spot Account
    # Примечание: API BingX v1 предоставляет только один эндпоинт для балансов, который, по-видимому,
    # объединяет средства со спотового и основного (Funding) счетов.
    current_app.logger.info("\n--- [BingX] Попытка получить баланс Spot Account ---")
    spot_data = _bingx_api_get(api_key, api_secret, '/openApi/spot/v1/account/balance')
    if spot_data and spot_data.get('code') == 0 and spot_data.get('data', {}).get('balances'):
        for asset_data in spot_data['data']['balances']:
            quantity = float(asset_data.get('free', 0)) + float(asset_data.get('locked', 0))
            if quantity > 1e-9:
                key = (asset_data['asset'], 'Spot')
                assets_map[key] = assets_map.get(key, 0.0) + quantity
    else:
        current_app.logger.debug(f"[BingX Debug] Raw spot_data response: {json.dumps(spot_data, indent=2) if spot_data else 'No response'}")
        current_app.logger.warning("[BingX] Не удалось получить баланс Spot Account или он пуст.")
    
    # Примечание: Получение балансов Funding и Earn для BingX отключено.
    # API не предоставляет отдельных эндпоинтов для этих кошельков.
    # Эндпоинт для Earn (/openApi/wealth/v1/savings/position) и Funding
    # возвращает ошибку "api is not exist". Это может быть связано с отсутствием
    # необходимых прав у API-ключа ("Wealth") или с тем, что эндпоинт устарел.
    current_app.logger.info("\n--- [BingX] Получение балансов Funding и Earn пропущено (API не предоставляет эндпоинты). ---")


    all_assets = []
    for (ticker, account_type), quantity in assets_map.items():
        all_assets.append({'ticker': ticker, 'quantity': str(quantity), 'account_type': account_type})
    return all_assets


# --- РЕФАКТОРИНГ: Классы для API клиентов ---

class BaseApiClient:
    """Базовый класс для всех API клиентов."""
    def __init__(self, api_key, api_secret, passphrase=None, base_url=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url

    def _get(self, path, params=None):
        raise NotImplementedError

    def _post(self, path, data=None, params=None):
        raise NotImplementedError

class BybitClient(BaseApiClient):
    """Клиент для работы с Bybit API v5."""
    def __init__(self, api_key, api_secret, passphrase=None):
        super().__init__(api_key, api_secret, passphrase, BYBIT_BASE_URL)
        self.time_offset = 0
        self.sync_time()

    def sync_time(self):
        """Синхронизирует локальное время с временем сервера Bybit."""
        try:
            url = f"{self.base_url}/v5/market/time"
            # Public endpoint, no auth needed
            response = _make_request('GET', url)
            if response and response.get('retCode') == 0:
                server_time_ms = int(response['result']['timeNano']) // 1_000_000
                local_time_ms = int(time.time() * 1000)
                self.time_offset = server_time_ms - local_time_ms
                current_app.logger.info(f"[Bybit Time Sync] Server time synced. Offset is {self.time_offset} ms.")
            else:
                current_app.logger.warning(f"[Bybit Time Sync] Failed to sync server time. Using local time.")
                self.time_offset = 0
        except Exception as e:
            current_app.logger.error(f"[Bybit Time Sync] Error syncing time: {e}. Using local time.")
            self.time_offset = 0

    def _request(self, method, path, params=None):
        """Выполняет подписанный запрос к Bybit."""
        timestamp = str(int(time.time() * 1000) + self.time_offset) # Use synchronized time
        recv_window = "20000"
        
        params_with_recv_window = params.copy() if params else {}
        params_with_recv_window['recvWindow'] = recv_window
        query_string = urlencode(dict(sorted(params_with_recv_window.items())))

        payload = f"{timestamp}{self.api_key}{recv_window}{query_string}"
        signature = hmac.new(self.api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()

        url = f"{self.base_url}{path}?{query_string}"
        headers = {
            'X-BAPI-API-KEY': self.api_key,
            'X-BAPI-TIMESTAMP': timestamp,
            'X-BAPI-RECV-WINDOW': recv_window,
            'X-BAPI-SIGN': signature,
            'Content-Type': 'application/json'
        }
        current_app.logger.info(f"\n--- [Bybit] Запрос к: {path} с параметрами {params} ---")
        return _make_request(method, url, headers=headers)

    def _get(self, path, params=None):
        return self._request('GET', path, params)

    def get_account_assets(self):
        """Получает балансы активов с Bybit, включая Funding и Earn."""
        assets_map = {}

        # 1. Unified Trading Account
        current_app.logger.info("\n--- [Bybit] Попытка получить баланс Unified Trading Account ---")
        try:
            unified_data = self._get('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})
            if unified_data.get('retCode') == 0 and unified_data.get('result', {}).get('list'):
                for coin_balance in unified_data['result']['list'][0].get('coin', []):
                    balance = float(coin_balance.get('walletBalance', 0))
                    if balance > 0:
                        key = (coin_balance['coin'], 'Unified Trading')
                        assets_map[key] = assets_map.get(key, 0.0) + balance
        except Exception as e:
            current_app.logger.error(f"Исключение при получении баланса Unified Account: {e}")

        # 2. Funding Account
        current_app.logger.info("\n--- [Bybit] Попытка получить баланс Funding Account ---")
        try:
            funding_data = self._get('/v5/asset/transfer/query-account-coins-balance', {'accountType': 'FUND'})
            if funding_data.get('retCode') == 0 and funding_data.get('result', {}).get('balance'):
                for coin_balance in funding_data['result']['balance']:
                    balance = float(coin_balance.get('walletBalance', 0))
                    if balance > 0:
                        key = (coin_balance['coin'], 'Funding')
                        assets_map[key] = assets_map.get(key, 0.0) + balance
        except Exception as e:
            current_app.logger.error(f"Исключение при получении баланса Funding Account: {e}")

        # 3. Earn Account
        current_app.logger.info("\n--- [Bybit] Попытка получить баланс Earn ---")
        earn_categories = ['FlexibleSaving', 'OnChain']
        for category in earn_categories:
            try:
                earn_data = self._get('/v5/earn/position', {'category': category})
                if earn_data.get('retCode') == 0 and earn_data.get('result', {}).get('list'):
                    for pos in earn_data['result']['list']:
                        principal = float(pos.get('amount', 0))
                        if principal > 1e-9:
                            key = (pos['coin'], 'Earn')
                            assets_map[key] = assets_map.get(key, 0.0) + principal
                elif earn_data.get('retCode') != 0:
                    current_app.logger.info(f"[Bybit] Информация: не удалось получить баланс Earn для категории {category}: {earn_data.get('retMsg')}.")
            except Exception as e:
                current_app.logger.error(f"Исключение при получении баланса Earn для категории {category}: {e}")

        return [{'ticker': t, 'quantity': str(q), 'account_type': at} for (t, at), q in assets_map.items() if q > 1e-9]

    def _fetch_paginated_history(self, endpoint, start_time_dt, end_time_dt, extra_params=None):
        """Общая функция для получения истории с пагинацией по времени и курсору."""
        # ИЗМЕНЕНО: Добавлена поддержка extra_params для гибкости
        all_records = []
        end_time = end_time_dt if end_time_dt else datetime.now(timezone.utc)
        limit_date = start_time_dt or (end_time - timedelta(days=2*365))

        while end_time > limit_date:
            start_time = end_time - timedelta(days=7)
            start_ts_ms = int(start_time.timestamp() * 1000)
            end_ts_ms = int(end_time.timestamp() * 1000)
            
            current_app.logger.info(f"--- [Bybit History: {endpoint}] Запрос за период: {start_time.strftime('%Y-%m-%d')} -> {end_time.strftime('%Y-%m-%d')}")
            
            cursor = ""
            history_limit_reached = False
            while True:
                params = {'limit': 50, 'startTime': start_ts_ms, 'endTime': end_ts_ms}
                if extra_params:
                    params.update(extra_params)
                if cursor:
                    params['cursor'] = cursor
                
                response_data = self._get(endpoint, params)
                ret_code = response_data.get('retCode')

                if ret_code == 10001:
                    current_app.logger.info(f"--- [Bybit History: {endpoint}] Достигнут предел истории в 2 года.")
                    history_limit_reached = True
                    break
                elif ret_code != 0:
                    raise Exception(f"Ошибка API Bybit для {endpoint}: {response_data.get('retMsg')}")

                result = response_data.get('result', {})
                records = result.get('rows', []) or result.get('list', [])
                if records:
                    all_records.extend(records)
                
                cursor = result.get('nextPageCursor')
                if not cursor:
                    break
            
            if history_limit_reached:
                break
            end_time = start_time
            time.sleep(0.2)
        return all_records

# --- Функции для получения балансов аккаунтов (требуют аутентификации) ---

def fetch_bybit_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с Bybit, включая Funding и Earn."""
    current_app.logger.info(f"Получение реальных балансов с Bybit (прямой API, включая Funding и Earn) с ключом: {api_key[:5]}...")
    client = BybitClient(api_key, api_secret)
    return client.get_account_assets()

class OKXClient(BaseApiClient):
    """Клиент для работы с OKX API v5."""
    def __init__(self, api_key, api_secret, passphrase):
        super().__init__(api_key, api_secret, passphrase, OKX_BASE_URL)

    def _request(self, method, path, params=None, data=None):
        """Выполняет подписанный запрос к OKX."""
        timestamp = datetime.utcnow().isoformat()[:-3] + 'Z'
        
        request_path = path
        if method.upper() == 'GET' and params:
            request_path += f"?{urlencode(params)}"
        
        body_str = ""
        if data:
            body_str = json.dumps(data)

        prehash = timestamp + method.upper() + request_path + body_str
        signature = base64.b64encode(hmac.new(self.api_secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
        
        headers = {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json'
        }
        
        url = f"{self.base_url}{path}"
        response_data = _make_request(method, url, headers=headers, params=params, data=body_str)
        
        if response_data.get('code') != '0':
            raise Exception(f"Ошибка API OKX для {path}: {response_data.get('msg')}")
        
        return response_data.get('data', [])

    def _get(self, path, params=None):
        return self._request('GET', path, params)

    def get_account_assets(self):
        """Получает балансы активов с OKX, включая Trading, Funding и Financial (Earn)."""
        assets_map = {}
        try:
            trading_data = self._get('/api/v5/account/balance')
            if trading_data:
                for asset_data in trading_data[0].get('details', []):
                    quantity = float(asset_data.get('cashBal', 0))
                    if quantity > 1e-9:
                        assets_map[(asset_data['ccy'], 'Trading')] = assets_map.get((asset_data['ccy'], 'Trading'), 0.0) + quantity
        except Exception as e:
            current_app.logger.error(f"Исключение при получении баланса OKX Trading Account: {e}")
        try:
            funding_data = self._get('/api/v5/asset/balances')
            if funding_data:
                for asset_data in funding_data:
                    quantity = float(asset_data.get('bal', 0))
                    if quantity > 1e-9:
                        assets_map[(asset_data['ccy'], 'Funding')] = assets_map.get((asset_data['ccy'], 'Funding'), 0.0) + quantity
        except Exception as e:
            current_app.logger.error(f"Исключение при получении баланса OKX Funding Account: {e}")
        try:
            financial_data = self._get('/api/v5/finance/savings/balance')
            if financial_data:
                for asset_data in financial_data:
                    quantity = float(asset_data.get('amt', 0))
                    if quantity > 1e-9:
                        assets_map[(asset_data['ccy'], 'Earn')] = assets_map.get((asset_data['ccy'], 'Earn'), 0.0) + quantity
        except Exception as e:
            current_app.logger.error(f"Исключение при получении баланса OKX Financial Account: {e}")
        return [{'ticker': t, 'quantity': str(q), 'account_type': at} for (t, at), q in assets_map.items()]

    def _fetch_paginated_data(self, endpoint, id_key, start_ts_ms, end_ts_ms, params=None):
        all_records = []
        last_id = None
        if params is None: params = {}
        if endpoint in ['/api/v5/asset/deposit-history', '/api/v5/asset/withdrawal-history']:
            if start_ts_ms: params['begin'] = start_ts_ms
            if end_ts_ms: params['end'] = end_ts_ms
        while True:
            if last_id: params['after'] = last_id
            records = self._get(endpoint, params)
            if not records: break
            all_records.extend(records)
            if len(records) < 100: break
            last_id = records[-1][id_key]
            time.sleep(0.2)
        return all_records

    def get_all_transactions(self, start_time_dt, end_time_dt):
        start_ts_ms = int(start_time_dt.timestamp() * 1000) if start_time_dt else None
        end_ts_ms = int(end_time_dt.timestamp() * 1000) if end_time_dt else None
        all_txs = {'deposits': [], 'withdrawals': [], 'trades': []}
        try: all_txs['deposits'] = self._fetch_paginated_data('/api/v5/asset/deposit-history', 'depId', start_ts_ms, end_ts_ms)
        except Exception as e: current_app.logger.error(f"Не удалось получить историю депозитов OKX: {e}")
        try: all_txs['withdrawals'] = self._fetch_paginated_data('/api/v5/asset/withdrawal-history', 'wdId', start_ts_ms, end_ts_ms)
        except Exception as e: current_app.logger.error(f"Не удалось получить историю выводов OKX: {e}")
        try:
            all_trades_raw = self._fetch_paginated_data('/api/v5/trade/fills-history', 'tradeId', start_ts_ms, end_ts_ms, params={'instType': 'SPOT'})
            all_txs['trades'] = [t for t in all_trades_raw if (not start_ts_ms or int(t.get('ts', 0)) >= start_ts_ms) and (not end_ts_ms or int(t.get('ts', 0)) <= end_ts_ms)]
        except Exception as e: current_app.logger.error(f"Не удалось получить историю сделок OKX: {e}")
        current_app.logger.info(f"--- [OKX History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок.")
        return all_txs

def fetch_bybit_deposit_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list:
    """Получает всю историю депозитов (зачислений) с Bybit."""
    current_app.logger.info(f"Получение истории депозитов с Bybit с ключом: {api_key[:5]}...")
    client = BybitClient(api_key, api_secret)
    all_deposits = client._fetch_paginated_history('/v5/asset/deposit/query-record', start_time_dt, end_time_dt)
    unique_deposits = list({d['txID']: d for d in all_deposits}.values())
    current_app.logger.info(f"--- [Bybit Deposits] Всего найдено {len(all_deposits)} депозитов, уникальных: {len(unique_deposits)}.")
    return unique_deposits

def fetch_bybit_internal_deposit_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list:
    """Получает всю историю внутренних депозитов (зачислений от других пользователей Bybit)."""
    current_app.logger.info(f"Получение истории внутренних депозитов с Bybit с ключом: {api_key[:5]}...")
    client = BybitClient(api_key, api_secret)
    all_deposits = client._fetch_paginated_history('/v5/asset/deposit/query-internal-record', start_time_dt, end_time_dt)
    unique_deposits = list({d['id']: d for d in all_deposits}.values())
    current_app.logger.info(f"--- [Bybit Internal Deposits] Всего найдено {len(all_deposits)} внутренних депозитов, уникальных: {len(unique_deposits)}.")
    return unique_deposits

def fetch_bybit_trade_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list: # Renamed from fetch_bybit_withdrawal_history
    """Получает всю историю спотовых сделок (покупок/продаж) с Bybit."""
    current_app.logger.info(f"Получение истории спотовых сделок с Bybit с ключом: {api_key[:5]}...")
    client = BybitClient(api_key, api_secret)
    # ИСПРАВЛЕНО: Передаем обязательный параметр 'category' для получения спотовых сделок.
    all_trades = client._fetch_paginated_history('/v5/execution/list', start_time_dt, end_time_dt, extra_params={'category': 'spot'})
    # Используем execId как уникальный идентификатор для сделок
    unique_trades = list({t['execId']: t for t in all_trades}.values())
    current_app.logger.info(f"--- [Bybit Trades] Всего найдено {len(all_trades)} сделок, уникальных: {len(unique_trades)}.")
    return unique_trades

def fetch_bybit_withdrawal_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list:
    """Получает всю историю выводов средств с Bybit."""
    current_app.logger.info(f"Получение истории выводов с Bybit с ключом: {api_key[:5]}...")
    client = BybitClient(api_key, api_secret)
    all_withdrawals = client._fetch_paginated_history('/v5/asset/withdraw/query-record', start_time_dt, end_time_dt)
    unique_withdrawals = list({w['txID']: w for w in all_withdrawals}.values())
    current_app.logger.info(f"--- [Bybit Withdrawals] Всего найдено {len(all_withdrawals)} выводов, уникальных: {len(unique_withdrawals)}.")
    return unique_withdrawals

def fetch_bybit_transfer_history(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> list:
    """Получает историю внутренних переводов с Bybit."""
    current_app.logger.info(f"Получение истории переводов с Bybit с ключом: {api_key[:5]}...")
    client = BybitClient(api_key, api_secret)
    all_transfers = client._fetch_paginated_history('/v5/asset/transfer/query-inter-transfer-list', start_time_dt, end_time_dt)
    # Удаляем дубликаты на случай пересечения временных рамок или особенностей API
    unique_transfers = list({t['transferId']: t for t in all_transfers}.values())
    current_app.logger.info(f"--- [Bybit History] Всего найдено {len(all_transfers)} транзакций, уникальных: {len(unique_transfers)}.")
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
        current_app.logger.error(f"Не удалось получить историю переводов Bybit: {e}")
    try:
        all_txs['deposits'] = fetch_bybit_deposit_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt)
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю депозитов Bybit: {e}")
    try:
        all_txs['internal_deposits'] = fetch_bybit_internal_deposit_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt)
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю внутренних депозитов Bybit: {e}")
        current_app.logger.error(f"--- [ERROR] Failed to fetch Bybit internal deposit history: {e}")
    try:
        all_txs['withdrawals'] = fetch_bybit_withdrawal_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt) # Correctly call and assign
    except Exception as e:
        current_app.logger.error(f"--- [ERROR] Failed to fetch Bybit withdrawal history: {e}")
        current_app.logger.error(f"Не удалось получить историю выводов Bybit: {e}")
    try:
        all_txs['trades'] = fetch_bybit_trade_history(api_key, api_secret, passphrase, start_time_dt, end_time_dt) # Вызываем новую функцию
    except Exception as e:
        current_app.logger.error(f"--- [ERROR] Failed to fetch Bybit trade history: {e}")
        current_app.logger.error(f"Не удалось получить историю сделок Bybit: {e}")
 
    return all_txs
def fetch_bitget_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с Bitget, включая Spot и Earn."""
    current_app.logger.info(f"Получение реальных балансов с Bitget (прямой API, включая Spot и Earn) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для Bitget необходимы API ключ, секрет и парольная фраза.")


    assets_map = {}

    # 1. Получаем баланс Spot Account
    current_app.logger.info("\n--- [Bitget] Попытка получить баланс Spot Account ---")
    spot_data = _bitget_api_get(api_key, api_secret, passphrase, '/api/v2/spot/account/assets')
    if spot_data:
        for asset_data in spot_data.get('data', []):
            quantity = float(asset_data.get('available', 0)) + float(asset_data.get('frozen', 0))
            if quantity > 1e-9:
                key = (asset_data['coin'], 'Spot')
                assets_map[key] = assets_map.get(key, 0.0) + quantity

    # 2. Получаем баланс Earn Account
    current_app.logger.info("\n--- [Bitget] Попытка получить баланс Earn Account ---")
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
    current_app.logger.info(f"Получение истории транзакций с Bitget с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для Bitget необходимы API ключ, секрет и парольная фраза.")

    start_ts_ms = int(start_time_dt.timestamp() * 1000) if start_time_dt else None
    end_ts_ms = int(end_time_dt.timestamp() * 1000) if end_time_dt else None

    def _fetch_paginated_data_with_time(endpoint, id_key_for_record, pagination_param_name, base_params=None):
        """Общая функция для получения данных с пагинацией Bitget."""
        all_records = []
        last_id = None
        
        current_params = base_params.copy() if base_params else {}
        # Устанавливаем временные рамки для первого запроса
        if start_ts_ms:
            current_params['startTime'] = start_ts_ms
        if end_ts_ms:
            current_params['endTime'] = end_ts_ms

        stop_fetching = False
        while not stop_fetching:
            current_params['limit'] = 100

            if last_id:
                current_params[pagination_param_name] = last_id
                # Удаляем временные параметры для последующих страниц, так как Bitget их игнорирует при наличии idLessThan
                current_params.pop('startTime', None)
                current_params.pop('endTime', None)
            
            response_data = _bitget_api_get(api_key, api_secret, passphrase, endpoint, current_params)
            if not response_data or not response_data.get('data'):
                break
            
            records = response_data['data']
            for record in records:
                record_ts = int(record.get('cTime', 0))
                if start_ts_ms and record_ts < start_ts_ms:
                    stop_fetching = True
                    break
                all_records.append(record)

            if stop_fetching or len(records) < 100:
                break
            
            last_id = records[-1].get(id_key_for_record)
            time.sleep(0.2)
        return all_records

    all_txs = {
        'deposits': [],
        'withdrawals': [],
        'trades': [],
        'transfers': []
    }
    
    # --- Deposits ---
    try:
        current_app.logger.info("\n--- [Bitget] Получение истории депозитов ---")
        all_txs['deposits'] = _fetch_paginated_data_with_time('/api/v2/asset/funding/deposit-record', 'id', 'idLessThan')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю депозитов Bitget: {e}")
        
    # --- Withdrawals ---
    try:
        current_app.logger.info("\n--- [Bitget] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_paginated_data_with_time('/api/v2/asset/funding/withdrawal-record', 'withdrawId', 'idLessThan')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю выводов Bitget: {e}")
        
    # --- Transfers ---
    try:
        current_app.logger.info("\n--- [Bitget] Получение истории переводов ---")
        # ИСПРАВЛЕНО: Путь для переводов также изменен для соответствия новой структуре API.
        all_txs['transfers'] = _fetch_paginated_data_with_time('/api/v2/asset/funding/transfer-record', 'id', 'idLessThan')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю переводов Bitget: {e}")
        
    # --- Trades ---
    try:
        current_app.logger.info("\n--- [Bitget] Получение истории сделок (используя /v2/spot/trade/fills) ---")
        # ВОЗВРАЩЕНО: Используем эндпоинт fills, который возвращает данные за последние 2 дня.
        # Пагинация по after=tradeId. Фильтрация по времени вручную.
        all_trades = []
        last_id = None
        stop_fetching = False
        while not stop_fetching:
            params = {'limit': 500} # Максимальный лимит
            if last_id:
                params['after'] = last_id
            
            response_data = _bitget_api_get(api_key, api_secret, passphrase, '/api/v2/spot/trade/fills', params)
            if not response_data or not response_data.get('data'):
                break
            
            trades_page = response_data['data']
            for trade in trades_page:
                trade_ts_ms = int(trade.get('cTime', 0))
                if start_ts_ms and trade_ts_ms < start_ts_ms:
                    stop_fetching = True
                    break
                all_trades.append(trade)

            if stop_fetching or len(trades_page) < 500:
                break
            last_id = trades_page[-1]['tradeId']
            time.sleep(0.2)
        all_txs['trades'] = all_trades
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю сделок Bitget: {e}")
    
    current_app.logger.info(f"--- [Bitget History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок, {len(all_txs['transfers'])} переводов.")
    return all_txs

def fetch_bingx_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> dict:
    """
    Агрегатор для получения всех типов транзакций с BingX (депозиты, выводы, сделки).
    """
    current_app.logger.info(f"Получение истории транзакций с BingX с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для BingX необходимы API ключ и секрет.")

    start_ts_ms = int(start_time_dt.timestamp() * 1000) if start_time_dt else None
    end_ts_ms = int(end_time_dt.timestamp() * 1000) if end_time_dt else None

    def _fetch_bingx_paginated_data(endpoint, time_key='time', start_time=None, end_time=None, extra_params=None):
        """
        Общая функция для получения данных с пагинацией BingX по времени.
        Использует итерацию по временным чанкам и пагинацию по ID для эндпоинта сделок.
        """
        all_records = []
        current_end_time = end_time if end_time is not None else int(time.time() * 1000)
        # По умолчанию запрашиваем историю за 2 года
        current_start_time = start_time if start_time is not None else current_end_time - (2 * 365 * 24 * 60 * 60 * 1000) # По умолчанию 2 года

        # BingX API позволяет запрашивать до 7 дней для некоторых эндпоинтов истории.
        chunk_size_ms = 7 * 24 * 60 * 60 * 1000 # 7 дней в миллисекундах

        while current_end_time > current_start_time:
            temp_start_time = max(current_start_time, current_end_time - chunk_size_ms)
            
            current_app.logger.info(f"--- [BingX History] Запрос за период: {datetime.fromtimestamp(temp_start_time / 1000, tz=timezone.utc).strftime('%Y-%m-%d')} -> {datetime.fromtimestamp(current_end_time / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}")

            last_id = None
            # Внутренний цикл для пагинации по fromId, если эндпоинт это поддерживает
            while True: 
                params = {
                    'startTime': temp_start_time,
                    'endTime': current_end_time,
                    'limit': 1000 # Используем максимальный лимит
                }
                if extra_params:
                    params.update(extra_params)
                
                # Для эндпоинта сделок используется fromId для пагинации
                if endpoint == '/openApi/spot/v1/trade/myTrades' and last_id:
                    params['fromId'] = last_id

                response_data = _bingx_api_get(api_key, api_secret, endpoint, params)
                
                if not response_data or not response_data.get('data'):
                    break # Нет данных, выходим из внутреннего цикла
                
                records = response_data['data']
                all_records.extend(records)
                
                # Проверяем, нужно ли продолжать пагинацию
                if len(records) < params['limit']:
                    break # Это была последняя страница для данного чанка
                
                # Устанавливаем last_id для следующей итерации, только для эндпоинта сделок
                if endpoint == '/openApi/spot/v1/trade/myTrades':
                    last_id = records[-1].get('id')
                    if not last_id:
                        current_app.logger.warning(f"Не удалось найти 'id' в последней записи для пагинации BingX. Прерывание пагинации для чанка.")
                        break
                else:
                    # Другие эндпоинты (депозиты/выводы) не поддерживают пагинацию по ID, поэтому выходим
                    break
                
                time.sleep(0.2)

            # Переходим к следующему временному чанку
            current_end_time = temp_start_time - 1 # Переходим к предыдущему чанку
            time.sleep(0.2) # Ограничение скорости запросов

        return all_records

    all_txs = {'deposits': [], 'withdrawals': [], 'trades': []}
    try:
        current_app.logger.info("\n--- [BingX] Получение истории депозитов ---")
        all_txs['deposits'] = _fetch_bingx_paginated_data('/openApi/wallets/v1/capital/deposit/history', 'insertTime', start_time=start_ts_ms, end_time=end_ts_ms)
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю депозитов BingX: {e}")
    try:
        current_app.logger.info("\n--- [BingX] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_bingx_paginated_data('/openApi/wallets/v1/capital/withdraw/history', 'applyTime', start_time=start_ts_ms, end_time=end_ts_ms)
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю выводов BingX: {e}")
    try:
        # ИСПРАВЛЕНО: Для получения сделок с BingX необходимо итерировать по всем торговым парам,
        # так как API требует обязательного указания параметра 'symbol'.
        def _get_all_bingx_symbols():
            """Вспомогательная функция для получения всех торговых пар с BingX."""
            try:
                current_app.logger.info("--- [BingX] Получение списка всех торговых пар...")
                response = _make_request('GET', f"{BINGX_BASE_URL}/openApi/spot/v1/common/symbols")
                if response and response.get('code') == 0:
                    symbols = [s['symbol'] for s in response.get('data', {}).get('symbols', [])]
                    current_app.logger.info(f"--- [BingX] Найдено {len(symbols)} торговых пар.")
                    return symbols
                current_app.logger.error(f"Не удалось получить список пар с BingX: {response.get('msg')}")
                return []
            except Exception as e:
                current_app.logger.error(f"Исключение при получении списка пар с BingX: {e}")
                return []

        all_symbols = _get_all_bingx_symbols()
        if not all_symbols:
            current_app.logger.warning("История сделок BingX не будет загружена, так как не удалось получить список торговых пар.")
            all_txs['trades'] = []
        else:
            all_trades = []
            for i, symbol in enumerate(all_symbols):
                if (i + 1) % 50 == 0:
                    current_app.logger.info(f"--- [BingX Trades] Обработано {i + 1}/{len(all_symbols)} торговых пар...")
                
                trades_for_symbol = _fetch_bingx_paginated_data(
                    '/openApi/spot/v1/trade/myTrades', 'time', start_time=start_ts_ms, 
                    end_time=end_ts_ms, extra_params={'symbol': symbol}
                )
                if trades_for_symbol:
                    all_trades.extend(trades_for_symbol)
                time.sleep(0.1) # Небольшая задержка для соблюдения лимитов API
            all_txs['trades'] = all_trades
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю сделок BingX: {e}", exc_info=True)
    
    current_app.logger.info(f"--- [BingX History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок.")
    return all_txs

def fetch_okx_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с OKX, используя OKXClient."""
    current_app.logger.info(f"Получение реальных балансов с OKX (прямой API) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для OKX необходимы API ключ, секрет и парольная фраза.")
    client = OKXClient(api_key, api_secret, passphrase)
    return client.get_account_assets()

def fetch_okx_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None) -> dict:
    """Получает все транзакции с OKX, используя OKXClient."""
    current_app.logger.info(f"Получение истории транзакций с OKX с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для OKX необходимы API ключ, секрет и парольная фраза.")
    client = OKXClient(api_key, api_secret, passphrase)
    return client.get_all_transactions(start_time_dt, end_time_dt)

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
            current_app.logger.warning(f"Предупреждение API KuCoin для {endpoint}: {response_data.get('msg')}")
            return None
        return response_data
    except Exception as e:
        current_app.logger.error(f"Исключение при запросе к KuCoin {endpoint}: {e}")
        return None

def fetch_kucoin_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list:
    """Получает балансы активов с KuCoin, включая Main, Trade и Earn."""
    current_app.logger.info(f"Получение реальных балансов с KuCoin (прямой API) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для KuCoin необходимы API ключ, секрет и парольная фраза.")

    assets_map = {}
    
    # KuCoin API возвращает все счета одним запросом
    current_app.logger.info("\n--- [KuCoin] Попытка получить балансы со всех счетов ---")
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
    ИСПРАВЛЕНО: Добавлена логика для обхода 24-часового ограничения API KuCoin
    путем итерации по временному диапазону с шагом в 24 часа.
    """
    current_app.logger.info(f"Получение истории транзакций с KuCoin с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для KuCoin необходимы API ключ, секрет и парольная фраза.")

    def _fetch_kucoin_paginated_data_in_chunks(endpoint, base_params=None):
        """
        Fetches paginated data from KuCoin by iterating through the time range in 24-hour chunks.
        This is necessary because the API limits time-based queries to a 24-hour window.
        """
        all_records = []
        
        # Определяем общий временной диапазон для загрузки
        loop_end_time = end_time_dt if end_time_dt else datetime.now(timezone.utc)
        # По умолчанию запрашиваем историю за 2 года, если начальная дата не указана
        loop_start_time = start_time_dt if start_time_dt else (loop_end_time - timedelta(days=2*365))

        current_chunk_end_time = loop_end_time

        while current_chunk_end_time > loop_start_time:
            # Определяем 24-часовой отрезок, не выходя за общие рамки
            current_chunk_start_time = max(loop_start_time, current_chunk_end_time - timedelta(hours=24))
            
            current_app.logger.info(f"--- [KuCoin History: {endpoint}] Запрос за чанк: {current_chunk_start_time.strftime('%Y-%m-%d %H:%M')} -> {current_chunk_end_time.strftime('%Y-%m-%d %H:%M')}")

            current_page = 1
            while True: # Цикл пагинации для текущего отрезка
                params = base_params.copy() if base_params else {}
                params['currentPage'] = current_page
                params['pageSize'] = 500
                params['startAt'] = int(current_chunk_start_time.timestamp() * 1000)
                params['endAt'] = int(current_chunk_end_time.timestamp() * 1000)

                response_data = _kucoin_api_get(api_key, api_secret, passphrase, endpoint, params)
                if not response_data or not response_data.get('data', {}).get('items'):
                    break # Больше нет данных на этой странице или в этом отрезке
                
                records = response_data['data']['items']
                all_records.extend(records)
                
                # Проверяем, была ли это последняя страница для данного отрезка
                if len(records) < params['pageSize']:
                    break
                
                current_page += 1
                time.sleep(0.3)
            
            # Переходим к предыдущему 24-часовому отрезку
            current_chunk_end_time = current_chunk_start_time - timedelta(microseconds=1)
            time.sleep(0.3)

        # Удаление дубликатов, если API вернет их на границах отрезков
        unique_records_dict = {}
        # Определяем уникальный ключ для каждого типа транзакции
        id_key_map = {'/api/v1/deposits': 'walletTxId', '/api/v1/withdrawals': 'id', '/api/v1/fills': 'tradeId', '/api/v2/accounts/ledgers': 'id'}
        id_key = id_key_map.get(endpoint)
        if not id_key:
            current_app.logger.warning(f"Ключ для дедупликации не найден для {endpoint}. Возможны дубликаты.")
            return all_records
        for record in all_records:
            unique_id = record.get(id_key)
            if unique_id is not None:
                unique_records_dict[unique_id] = record
            else:
                unique_records_dict[json.dumps(record, sort_keys=True)] = record
        return list(unique_records_dict.values())

    all_txs = {'deposits': [], 'withdrawals': [], 'trades': [], 'transfers': []}
    try:
        current_app.logger.info("\n--- [KuCoin] Получение истории депозитов ---")
        all_txs['deposits'] = _fetch_kucoin_paginated_data_in_chunks('/api/v1/deposits')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю депозитов KuCoin: {e}")
    try:
        current_app.logger.info("\n--- [KuCoin] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_kucoin_paginated_data_in_chunks('/api/v1/withdrawals')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю выводов KuCoin: {e}")
    try:
        current_app.logger.info("\n--- [KuCoin] Получение истории сделок ---")
        all_txs['trades'] = _fetch_kucoin_paginated_data_in_chunks('/api/v1/fills')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю сделок KuCoin: {e}")
    try:
        current_app.logger.info("\n--- [KuCoin] Получение истории переводов (ledgers) ---")
        # ИСПРАВЛЕНО: Используем эндпоинт v1, так как v2 был объявлен устаревшим (deprecated).
        # Фильтруем по bizType, чтобы получить только переводы.
        all_txs['transfers'] = _fetch_kucoin_paginated_data_in_chunks('/api/v1/accounts/ledgers', base_params={'bizType': 'TRANSFER'})
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю переводов KuCoin: {e}")
    
    current_app.logger.info(f"--- [KuCoin History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок, {len(all_txs['transfers'])} переводов.")
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
