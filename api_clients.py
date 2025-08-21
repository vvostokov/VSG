import hmac
import hashlib
import base64
import json
import time
import requests
from datetime import datetime, timedelta, timezone, date # noqa
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Константы базовых URL API ---
BYBIT_BASE_URL = "https://api.bybit.com"
BITGET_BASE_URL = "https://api.bitget.com"
BINGX_BASE_URL = "https://open-api.bingx.com"
KUCOIN_BASE_URL = "https://api.kucoin.com"
OKX_BASE_URL = "https://www.okx.com"

from flask import current_app
from extensions import db

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
    # ИСПРАВЛЕНО: Логика генерации подписи полностью переписана для точного соответствия
    # требованиям BingX и решения проблемы "Signature verification failed".
    # Ключевое изменение: используется ручное формирование строки для подписи,
    # чтобы избежать расхождений, которые могут возникнуть при использовании urlencode.
    
    # 1. Подготовка параметров для подписи.
    params_for_signing = params.copy() if params else {}
    params_for_signing['timestamp'] = _get_timestamp_ms()
    params_for_signing['apiKey'] = api_key

    # 2. Сортировка параметров и создание строки для подписи.
    sorted_params = sorted(params_for_signing.items())
    query_string_to_sign = "&".join([f"{k}={v}" for k, v in sorted_params])

    # 3. Генерация подписи.
    signature = hmac.new(api_secret.encode('utf-8'), query_string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    # 4. Формирование финального URL. Подпись добавляется в конец.
    final_url = f"{BINGX_BASE_URL}{endpoint}?{query_string_to_sign}&signature={signature}"
    
    # 5. Формирование заголовков.
    headers = {'X-BX-APIKEY': api_key}

    try:
        # 6. Выполнение запроса. Передаем None в params, так как они уже включены в URL.
        response_data = _make_request('GET', final_url, headers=headers, params=None)
        
        # ИЗМЕНЕНО: Обрабатываем случай, когда API возвращает список напрямую, а не объект.
        # Это делает обработку ответов от BingX единообразной.
        if isinstance(response_data, list):
            return {'code': 0, 'data': response_data}

        if response_data.get('code') != 0: # BingX использует 0 для успеха
            # ИЗМЕНЕНО: Если API явно говорит, что эндпоинт не существует, логируем это как ошибку, а не предупреждение.
            if 'not exist' in response_data.get('msg', ''):
                current_app.logger.error(f"Ошибка API BingX для {endpoint}: {response_data.get('msg')}. Проверьте права API-ключа (требуется 'Read' для Wallet и Spot).")
            else:
                current_app.logger.warning(f"Предупреждение API BingX для {endpoint}: {response_data.get('msg')}")
            return None
        return response_data
    except Exception as e:
        current_app.logger.error(f"Исключение при запросе к BingX {endpoint}: {e}", exc_info=True)
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

def fetch_cbr_usd_rub_rate() -> Decimal | None:
    """Получает курс USD к RUB с сайта ЦБ РФ."""
    try:
        # Дата в формате, который требует ЦБ: dd/mm/YYYY
        today_str = datetime.now(timezone.utc).strftime('%d/%m/%Y')
        url = f"https://www.cbr.ru/scripts/XML_daily.asp?date_req={today_str}"
        current_app.logger.info(f"--- [Exchange Rate] Запрос курса USD/RUB с ЦБ РФ: {url}")
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        # Парсим XML
        root = ET.fromstring(response.content)
        # Ищем валюту с кодом 'USD' (ID R01235)
        usd_node = root.find("./Valute[@ID='R01235']")
        if usd_node is not None:
            value_str = usd_node.find('Value').text
            # Заменяем запятую на точку для конвертации в Decimal
            rate = Decimal(value_str.replace(',', '.'))
            current_app.logger.info(f"--- [Exchange Rate] Успешно получен курс USD/RUB от ЦБ РФ: {rate}")
            return rate
        else:
            current_app.logger.warning("--- [Exchange Rate] Не удалось найти курс USD в ответе от ЦБ РФ.")
            return None
    except Exception as e:
        current_app.logger.error(f"--- [Exchange Rate] Ошибка при получении курса от ЦБ РФ: {e}")
        return None

def fetch_usdt_rub_rate() -> Decimal | None:
    """
    Получает актуальный курс USDT к RUB.
    Сначала пытается получить курс USD/RUB от ЦБ РФ как наиболее надежный источник.
    В случае неудачи, возвращает None.
    """
    # 1. Попытка получить курс с ЦБ РФ (самый надежный)
    cbr_rate = fetch_cbr_usd_rub_rate()
    if cbr_rate:
        return cbr_rate

    # Если ЦБ РФ недоступен, возвращаем None, чтобы сработал fallback в вызывающем коде.
    return None

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

def fetch_bybit_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None, platform=None) -> dict:
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
        raise Exception("Для Bitget необходимы API ключ, секрет и парольная фраза.") # noqa


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
def fetch_bitget_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None, platform=None) -> dict:
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
            
            # ИЗМЕНЕНО: Корректная обработка разной структуры ответа API.
            # Для сделок данные лежат в data['fills'], для остального - просто в data.
            data_content = response_data['data']
            records = []
            if endpoint == '/openApi/spot/v1/trade/myTrades':
                records = data_content.get('fills', [])
            elif isinstance(data_content, list):
                records = data_content
            
            if not records:
                break # Выходим, если данных нет
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
        all_txs['deposits'] = _fetch_paginated_data_with_time('/api/v2/spot/wallet/deposit-records', 'id', 'idLessThan')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю депозитов Bitget: {e}")
        
    # --- Withdrawals ---
    try:
        current_app.logger.info("\n--- [Bitget] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_paginated_data_with_time('/api/v2/spot/wallet/withdrawal-records', 'withdrawId', 'idLessThan')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю выводов Bitget: {e}")
        
    # --- Transfers ---
    try:
        current_app.logger.info("\n--- [Bitget] Получение истории переводов ---")
        all_txs['transfers'] = _fetch_paginated_data_with_time('/api/v2/asset/transfer-records', 'id', 'idLessThan')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю переводов Bitget: {e}")
        
    # --- Trades ---
    try:
        current_app.logger.info("\n--- [Bitget] Получение истории сделок (используя /api/v2/spot/trade/fills) ---")
        all_txs['trades'] = _fetch_paginated_data_with_time('/api/v2/spot/trade/fills', 'tradeId', 'idLessThan')
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю сделок Bitget: {e}")
    
    current_app.logger.info(f"--- [Bitget History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок, {len(all_txs['transfers'])} переводов.")
    return all_txs

def fetch_bingx_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None, platform=None) -> dict: # noqa
    """
    Агрегатор для получения всех типов транзакций с BingX (депозиты, выводы, сделки).
    ОПТИМИЗИРОВАНО: Запрашивает историю сделок только для тех пар, которые есть у пользователя.
    """
    current_app.logger.info(f"Получение истории транзакций с BingX (оптимизированный режим) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret:
        raise Exception("Для BingX необходимы API ключ и секрет.")

    start_ts_ms = int(start_time_dt.timestamp() * 1000) if start_time_dt else None
    end_ts_ms = int(end_time_dt.timestamp() * 1000) if end_time_dt else None

    def _fetch_bingx_paginated_data(endpoint, start_time=None, end_time=None, extra_params=None):
        """
        УПРОЩЕНО: Функция для получения данных с BingX.
        Для сделок ('myTrades') используется пагинация по 'fromId'.
        Для депозитов/выводов пагинация по времени не требуется, так как API возвращает все за 90 дней.
        """
        all_records = []
        last_id = None
        while True:
            params = {'limit': 1000}
            if start_time: params['startTime'] = start_time
            if end_time: params['endTime'] = end_time
            if extra_params: params.update(extra_params)
            if last_id: params['fromId'] = last_id

            response_data = _bingx_api_get(api_key, api_secret, endpoint, params)
            if not response_data or not response_data.get('data'):
                break
            
            # ИЗМЕНЕНО: Корректная обработка разной структуры ответа API.
            # Для сделок данные лежат в data['fills'], для остального - просто в data.
            data_content = response_data['data']
            records = []
            if endpoint == '/openApi/spot/v1/fills':
                records = data_content.get('fills', [])
            elif isinstance(data_content, list):
                records = data_content
            
            if not records:
                break # Выходим, если данных нет

            all_records.extend(records)
            
            # Пагинация по ID поддерживается только для myTrades
            if endpoint == '/openApi/spot/v1/fills':
                if len(records) < params['limit']:
                    break
                last_id = records[-1].get('id') if records else None
                if not last_id: break
            else:
                # Для других эндпоинтов выходим после первого запроса
                break
            
            time.sleep(0.3) # Пауза между страницами

        return all_records

    all_txs = {'deposits': [], 'withdrawals': [], 'trades': []}
    try:
        current_app.logger.info("\n--- [BingX] Получение истории депозитов ---")
        all_txs['deposits'] = _fetch_bingx_paginated_data('/openApi/wallets/v1/capital/deposit/history', start_time=start_ts_ms, end_time=end_ts_ms)
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю депозитов BingX: {e}")
    try:
        current_app.logger.info("\n--- [BingX] Получение истории выводов ---")
        all_txs['withdrawals'] = _fetch_bingx_paginated_data('/openApi/wallets/v1/capital/withdraw/history', start_time=start_ts_ms, end_time=end_ts_ms)
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю выводов BingX: {e}")
    try:
        # --- ИЗМЕНЕНО: Оптимизация получения истории сделок ---
        current_app.logger.info("\n--- [BingX] Получение истории сделок (оптимизированный режим) ---")
        if not platform:
            raise ValueError("Для оптимизированной синхронизации сделок BingX требуется объект платформы.")

        # --- ИЗМЕНЕНО: Более надежный способ сбора всех когда-либо использовавшихся тикеров ---
        # 1. Получаем тикеры из текущих/прошлых активов (даже с нулевым балансом)
        asset_tickers = {asset.ticker for asset in platform.assets}
        current_app.logger.info(f"--- [BingX Trades] Тикеры из InvestmentAsset: {asset_tickers}")

        # 2. Получаем тикеры из уже существующих транзакций на этой платформе
        tx_tickers_asset1 = {r[0] for r in db.session.query(Transaction.asset1_ticker).filter(Transaction.platform_id == platform.id, Transaction.asset1_ticker.isnot(None)).distinct().all()}
        tx_tickers_asset2 = {r[0] for r in db.session.query(Transaction.asset2_ticker).filter(Transaction.platform_id == platform.id, Transaction.asset2_ticker.isnot(None)).distinct().all()}
        current_app.logger.info(f"--- [BingX Trades] Тикеры из существующих транзакций: {tx_tickers_asset1.union(tx_tickers_asset2)}")
        
        # 3. Объединяем все источники для получения полного списка
        user_tickers = asset_tickers.union(tx_tickers_asset1).union(tx_tickers_asset2)
        current_app.logger.info(f"--- [BingX Trades] Итоговый список тикеров для проверки: {user_tickers}")
        if not user_tickers:
            current_app.logger.info("--- [BingX Trades] У пользователя нет активов или транзакций на этой платформе, история сделок не запрашивается.")
            all_txs['trades'] = []
        else:
            # ИЗМЕНЕНО: Генерируем только валидные торговые пары, где вторая валюта - одна из основных.
            quote_currencies = ['USDT', 'USDC', 'BTC', 'ETH']
            symbols_to_check = set()
            for ticker in user_tickers:
                for quote in quote_currencies:
                    # ИЗМЕНЕНО: Правильная проверка, чтобы избежать только идентичных пар (например, USDT-USDT),
                    # но разрешить пары, где базовый актив - одна из основных валют (например, ETH-USDT).
                    if ticker == quote: continue
                    symbols_to_check.add(f"{ticker}-{quote}")
            current_app.logger.info(f"--- [BingX Trades] Будут проверены следующие пары: {symbols_to_check}")

            all_trades = []
            app = current_app._get_current_object()
            
            def _fetch_trades_for_symbol_worker(symbol):
                with app.app_context():
                    return _fetch_bingx_paginated_data('/openApi/spot/v1/fills', start_time=start_ts_ms, end_time=end_ts_ms, extra_params={'symbol': symbol})

            with ThreadPoolExecutor(max_workers=2) as executor:
                future_to_symbol = {}
                for symbol in symbols_to_check:
                    future = executor.submit(_fetch_trades_for_symbol_worker, symbol)
                    future_to_symbol[future] = symbol
                    time.sleep(0.3) # ИЗМЕНЕНО: Добавляем задержку 300мс между запросами, чтобы избежать rate limit.
                
                for future in as_completed(future_to_symbol):
                    symbol = future_to_symbol[future]
                    try:
                        trades_for_symbol = future.result()
                        if trades_for_symbol:
                            current_app.logger.info(f"--- [BingX Trades] Найдено {len(trades_for_symbol)} сделок для пары {symbol}.")
                            all_trades.extend(trades_for_symbol)
                    except Exception as exc:
                        if 'symbol is invalid' not in str(exc):
                             current_app.logger.error(f'--- [BingX Worker] Ошибка при загрузке сделок для {symbol}: {exc}')
            
            all_txs['trades'] = all_trades
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю сделок BingX: {e}", exc_info=True)

    current_app.logger.info(f"--- [BingX History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок.")
    return all_txs

def fetch_okx_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list: # noqa
    """Получает балансы активов с OKX, используя OKXClient."""
    current_app.logger.info(f"Получение реальных балансов с OKX (прямой API) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для OKX необходимы API ключ, секрет и парольная фраза.")
    client = OKXClient(api_key, api_secret, passphrase)
    return client.get_account_assets()
def fetch_okx_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None, platform=None) -> dict:
    """Получает все транзакции с OKX, используя OKXClient."""
    current_app.logger.info(f"Получение истории транзакций с OKX с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для OKX необходимы API ключ, секрет и парольная фраза.")
    client = OKXClient(api_key, api_secret, passphrase)
    return client.get_all_transactions(start_time_dt, end_time_dt)

def _kucoin_api_get(api_key: str, api_secret: str, passphrase: str, endpoint: str, params: dict = None): # noqa
    """Внутренняя функция для выполнения GET-запросов к KuCoin с подписью."""
    timestamp = _get_timestamp_ms()
    method = 'GET'
    
    query_string = f"?{urlencode(params)}" if params else ""
    request_path = f"{endpoint}{query_string}"
    
    prehash = timestamp + method + request_path
    signature = base64.b64encode(hmac.new(api_secret.encode('utf-8'), prehash.encode('utf-8'), hashlib.sha256).digest()).decode('utf-8')
    
    # ИСПРАВЛЕНО: KuCoin требует, чтобы парольная фраза была зашифрована с помощью HMAC-SHA256,
    # используя API Secret в качестве ключа, а затем результат был закодирован в Base64.
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

def fetch_kucoin_account_assets(api_key: str, api_secret: str, passphrase: str = None) -> list: # noqa
    """Получает балансы активов с KuCoin, включая Main, Trade и Earn."""
    current_app.logger.info(f"Получение реальных балансов с KuCoin (прямой API) с ключом: {api_key[:5]}...")
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для KuCoin необходимы API ключ, секрет и парольная фраза.")

    assets_map = {}
    
    # KuCoin API возвращает все счета одним запросом
    current_app.logger.info("\n--- [KuCoin] Попытка получить балансы со всех счетов ---")
    # ИСПРАВЛЕНО: Используем эндпоинт v1, так как v2 возвращает ошибку 404.
    all_accounts_data = _kucoin_api_get(api_key, api_secret, passphrase, '/api/v1/accounts')
    if all_accounts_data and all_accounts_data.get('data'):
        for account in all_accounts_data['data']:
            quantity = float(account.get('balance', 0))
            if quantity > 1e-9:
                # ИСПРАВЛЕНО: Приводим тип счета к нижнему регистру для совместимости
                # с ответами v1 ('main') и v2 ('MAIN').
                account_type_raw = account.get('type', 'unknown').lower()
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

def fetch_kucoin_all_transactions(api_key: str, api_secret: str, passphrase: str = None, start_time_dt: datetime = None, end_time_dt: datetime = None, platform=None) -> dict:
    """
    Агрегатор для получения всех типов транзакций с KuCoin (депозиты, выводы, сделки).
    ИСПРАВЛЕНО: Добавлена логика для обхода 24-часового ограничения API KuCoin
    путем итерации по временному диапазону с шагом в 24 часа.
    """
    current_app.logger.info(f"Получение истории транзакций с KuCoin (параллельный режим) с ключом: {api_key[:5]}...") # noqa
    if not api_key or not api_secret or not passphrase:
        raise Exception("Для KuCoin необходимы API ключ, секрет и парольная фраза.")

    # ИСПРАВЛЕНО: Получаем реальный объект приложения, чтобы передать его контекст в другие потоки.
    app = current_app._get_current_object()

    def _fetch_single_kucoin_chunk(args):
        """
        (Внутренняя функция для ThreadPoolExecutor) Получает все страницы данных для одного временного отрезка.
        """
        # ИСПРАВЛЕНО: Создаем контекст приложения, так как эта функция выполняется в отдельном потоке.
        with app.app_context():
            endpoint, base_params, chunk_start_time, chunk_end_time = args
            chunk_records = []
            current_page = 1
            while True:
                params = base_params.copy() if base_params else {}
                params['currentPage'] = current_page
                params['pageSize'] = 500
                params['startAt'] = int(chunk_start_time.timestamp() * 1000)
                params['endAt'] = int(chunk_end_time.timestamp() * 1000)

                response_data = _kucoin_api_get(api_key, api_secret, passphrase, endpoint, params)
                if not response_data or not response_data.get('data', {}).get('items'):
                    break
                
                records = response_data['data']['items']
                chunk_records.extend(records)
                
                if len(records) < params['pageSize']:
                    break
                
                current_page += 1
                time.sleep(0.3) # Задержка для соблюдения rate limit
            return chunk_records

    def _fetch_kucoin_paginated_data_in_chunks(endpoint, base_params=None):
        """
        ОПТИМИЗИРОВАНО: Получает данные с KuCoin, запрашивая 24-часовые отрезки параллельно.
        """
        # 1. Генерируем список всех 24-часовых отрезков для запроса
        time_chunks = []
        loop_end_time = end_time_dt or datetime.now(timezone.utc)
        loop_start_time = start_time_dt or (loop_end_time - timedelta(days=2*365))
        current_chunk_end_time = loop_end_time
        # ИСПРАВЛЕНО: Увеличиваем размер чанка до 7 дней, так как API KuCoin это позволяет.
        # Это значительно сократит количество запросов.
        chunk_delta = timedelta(days=7)
        while current_chunk_end_time > loop_start_time:
            current_chunk_start_time = max(loop_start_time, current_chunk_end_time - chunk_delta)
            time_chunks.append((current_chunk_start_time, current_chunk_end_time))
            current_chunk_end_time = current_chunk_start_time - timedelta(microseconds=1)

        # 2. Запускаем запросы для всех отрезков параллельно
        all_records = []
        # ИСПРАВЛЕНО: Уменьшаем количество параллельных воркеров до 2, чтобы избежать ошибок 429 (Rate Limit).
        with ThreadPoolExecutor(max_workers=2) as executor:
            # Подготавливаем аргументы для каждой задачи
            tasks_args = [(endpoint, base_params, start, end) for start, end in time_chunks]
            future_to_chunk = {executor.submit(_fetch_single_kucoin_chunk, args): args for args in tasks_args}
            
            for i, future in enumerate(as_completed(future_to_chunk)):
                chunk_args = future_to_chunk[future]
                try:
                    chunk_result = future.result()
                    if chunk_result:
                        all_records.extend(chunk_result)
                    current_app.logger.info(f"--- [KuCoin Worker] Чанк {chunk_args[2].strftime('%Y-%m-%d')} для {endpoint} успешно загружен ({i+1}/{len(time_chunks)}).")
                except Exception as exc:
                    current_app.logger.error(f'--- [KuCoin Worker] Ошибка при загрузке чанка {chunk_args}: {exc}')

        # 3. Удаляем дубликаты, которые могли появиться на границах отрезков
        unique_records_dict = {}
        # ИСПРАВЛЕНО: Обновлен ключ для /api/v1/accounts/ledgers
        id_key_map = {
            '/api/v1/deposits': 'walletTxId', 
            '/api/v1/withdrawals': 'id', 
            '/api/v1/fills': 'tradeId', 
            '/api/v1/accounts/ledgers': 'id'
        }
        id_key = id_key_map.get(endpoint)
        if not id_key:
            current_app.logger.warning(f"Ключ для дедупликации не найден для {endpoint}. Возможны дубликаты.")
            return all_records
            
        for record in all_records:
            unique_id = record.get(id_key)
            if unique_id is not None:
                unique_records_dict[unique_id] = record
            else:
                # Резервный вариант для записей без уникального ID
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
        # Фильтруем по bizType, чтобы получить только переводы. Используем эндпоинт v1.
        all_txs['transfers'] = _fetch_kucoin_paginated_data_in_chunks('/api/v1/accounts/ledgers', base_params={'bizType': 'TRANSFER'})
    except Exception as e:
        current_app.logger.error(f"Не удалось получить историю переводов KuCoin: {e}")
    
    current_app.logger.info(f"--- [KuCoin History] Найдено: {len(all_txs['deposits'])} депозитов, {len(all_txs['withdrawals'])} выводов, {len(all_txs['trades'])} сделок, {len(all_txs['transfers'])} переводов.")
    return all_txs

# --- РЕФАКТОРИНГ: Классы-обработчики для синхронизации транзакций ---

class BaseTransactionProcessor:
    """Базовый класс для обработки транзакций с биржи."""
    def __init__(self, platform, existing_tx_ids):
        self.platform = platform
        self.existing_tx_ids = existing_tx_ids
        self.added_count = 0

    def process(self, fetched_data):
        """Основной метод, запускающий обработку всех типов транзакций."""
        self.process_deposits(fetched_data.get('deposits', []))
        self.process_internal_deposits(fetched_data.get('internal_deposits', []))
        self.process_withdrawals(fetched_data.get('withdrawals', []))
        self.process_transfers(fetched_data.get('transfers', []))
        self.process_trades(fetched_data.get('trades', []))

    def _add_transaction(self, tx_data):
        """Вспомогательный метод для добавления новой транзакции в сессию."""
        if tx_data['exchange_tx_id'] not in self.existing_tx_ids:
            db.session.add(Transaction(platform_id=self.platform.id, **tx_data))
            self.added_count += 1

    # Методы-заглушки, которые будут переопределены в дочерних классах
    def process_deposits(self, data): pass
    def process_internal_deposits(self, data): pass
    def process_withdrawals(self, data): pass
    def process_transfers(self, data): pass
    def process_trades(self, data): pass

class BybitTransactionProcessor(BaseTransactionProcessor):
    def process_deposits(self, data):
        for d in data:
            if d.get('status') == 1:
                self._add_transaction(dict(exchange_tx_id=f"bybit_deposit_{d['txID']}", timestamp=_convert_bybit_timestamp(d['successAt']), type='deposit', raw_type=f"Deposit via {d['chain']}", asset1_ticker=d['coin'], asset1_amount=Decimal(d['amount'])))

    def process_internal_deposits(self, data):
        for d in data:
            if d.get('status') in [1, 2]:
                self._add_transaction(dict(exchange_tx_id=f"bybit_internal_deposit_{d['id']}", timestamp=_convert_bybit_timestamp(d['createdTime']), type='deposit', raw_type='Internal Deposit', asset1_ticker=d['coin'], asset1_amount=Decimal(d['amount'])))

    def process_withdrawals(self, data):
        for w in data:
            if w.get('status') == 2:
                self._add_transaction(dict(exchange_tx_id=f"bybit_withdrawal_{w['txID']}", timestamp=_convert_bybit_timestamp(w['updateAt']), type='withdrawal', raw_type=f"Withdrawal ({w.get('withdrawType', 'N/A')})", asset1_ticker=w['coin'], asset1_amount=Decimal(w['amount']), fee_amount=Decimal(w.get('fee', '0')), fee_currency=w['coin']))

    def process_transfers(self, data):
        for t in data:
            self._add_transaction(dict(exchange_tx_id=f"bybit_transfer_{t['transferId']}", timestamp=_convert_bybit_timestamp(t['timestamp']), type='transfer', raw_type=f"{t['fromAccountType']} -> {t['toAccountType']}", asset1_ticker=t['coin'], asset1_amount=Decimal(t['amount']), description=f"Internal transfer on {self.platform.name}"))

    def process_trades(self, data):
        for trade in data:
            symbol = trade.get('symbol')
            known_quotes = ['USDT', 'USDC', 'BTC', 'ETH', 'BUSD', 'TUSD', 'DAI']
            base_coin, quote_coin = None, None
            if symbol:
                for quote in known_quotes:
                    if symbol.endswith(quote):
                        base_coin, quote_coin = symbol[:-len(quote)], quote
                        break
            if not base_coin: continue
            
            self._add_transaction(dict(
                exchange_tx_id=f"bybit_trade_{trade['execId']}", timestamp=_convert_bybit_timestamp(trade['execTime']), type=trade['side'].lower(), raw_type=f"Spot Trade ({trade['side'].upper()})",
                asset1_ticker=base_coin, asset1_amount=Decimal(trade.get('execQty', '0')),
                asset2_ticker=quote_coin, asset2_amount=Decimal(trade.get('execValue', '0')),
                execution_price=Decimal(trade.get('execPrice', '0')),
                fee_amount=Decimal(trade.get('execFee', '0')), fee_currency=trade.get('feeTokenId', quote_coin)
            ))

class BitgetTransactionProcessor(BaseTransactionProcessor):
    def process_deposits(self, data):
        for d in data:
            if d.get('status') == 'success':
                self._add_transaction(dict(exchange_tx_id=f"bitget_deposit_{d['id']}", timestamp=_convert_bybit_timestamp(d['cTime']), type='deposit', raw_type='Deposit', asset1_ticker=d['coin'], asset1_amount=Decimal(d['amount'])))

    def process_withdrawals(self, data):
        for w in data:
            if w.get('status') == 'success':
                self._add_transaction(dict(exchange_tx_id=f"bitget_withdrawal_{w['withdrawId']}", timestamp=_convert_bybit_timestamp(w['cTime']), type='withdrawal', raw_type='Withdrawal', asset1_ticker=w['coin'], asset1_amount=Decimal(w['amount']), fee_amount=Decimal(w.get('fee', '0')), fee_currency=w['coin']))

    def process_transfers(self, data):
        for t in data:
            if t.get('status') == 'success':
                self._add_transaction(dict(exchange_tx_id=f"bitget_transfer_{t['id']}", timestamp=_convert_bybit_timestamp(t['cTime']), type='transfer', raw_type=f"{t.get('fromType', 'N/A')} -> {t.get('toType', 'N/A')}", asset1_ticker=t['coin'], asset1_amount=Decimal(t['amount']), description=f"Internal transfer on {self.platform.name}"))

    def process_trades(self, data):
        for trade in data:
            symbol = trade.get('symbol')
            if not symbol: continue
            known_quotes = ['USDT', 'USDC', 'BTC', 'ETH', 'BUSD', 'TUSD', 'DAI']
            base_coin, quote_coin = next(((symbol[:-len(q)], q) for q in known_quotes if symbol.endswith(q)), (None, None))
            if not base_coin: continue

            asset1_amount = Decimal(trade.get('size', '0'))
            asset2_amount = Decimal(trade.get('amount', '0'))
            execution_price = Decimal(trade.get('price', '0'))
            if execution_price == 0 and asset1_amount > 0: execution_price = asset2_amount / asset1_amount

            fee_amount, fee_currency = self._parse_bitget_fee(trade, quote_coin)
            
            self._add_transaction(dict(
                exchange_tx_id=f"bitget_trade_{trade['tradeId']}", timestamp=_convert_bybit_timestamp(trade['cTime']), type=trade['side'].lower(), raw_type=f"Spot Trade ({trade['side'].upper()})",
                asset1_ticker=base_coin, asset1_amount=asset1_amount, asset2_ticker=quote_coin, asset2_amount=asset2_amount,
                execution_price=execution_price, fee_amount=fee_amount, fee_currency=fee_currency
            ))

    def _parse_bitget_fee(self, trade, default_fee_currency):
        fee_detail_value = trade.get('feeDetail')
        fee_details_list = None
        if isinstance(fee_detail_value, str) and fee_detail_value:
            try: fee_details_list = json.loads(fee_detail_value)
            except json.JSONDecodeError: pass
        elif isinstance(fee_detail_value, list):
            fee_details_list = fee_detail_value

        if fee_details_list and isinstance(fee_details_list, list) and len(fee_details_list) > 0:
            fee_details = fee_details_list[0]
            if isinstance(fee_details, dict):
                fee_amount = abs(Decimal(fee_details.get('fee', '0')))
                if fee_amount > 0:
                    return fee_amount, fee_details.get('feeCoin', default_fee_currency)

        if 'fee' in trade and 'feeCoin' in trade:
            fee_amount = abs(Decimal(trade.get('fee', '0')))
            if fee_amount > 0:
                return fee_amount, trade.get('feeCoin', default_fee_currency)
        
        return Decimal('0'), default_fee_currency

class BingxTransactionProcessor(BaseTransactionProcessor):
    def process_deposits(self, data):
        for d in data:
            # Адаптация под ответ API v1
            if d.get('status') == 1: # 1 - Success
                self._add_transaction(dict(exchange_tx_id=f"bingx_deposit_{d['id']}", timestamp=_convert_bybit_timestamp(d['insertTime']), type='deposit', raw_type='Deposit', asset1_ticker=d['asset'], asset1_amount=Decimal(d['amount'])))

    def process_withdrawals(self, data):
        for w in data:
            # Адаптация под ответ API v1
            if w.get('status') == 1: # 1 - Success
                self._add_transaction(dict(exchange_tx_id=f"bingx_withdrawal_{w['id']}", timestamp=_convert_bybit_timestamp(w['applyTime']), type='withdrawal', raw_type='Withdrawal', asset1_ticker=w['asset'], asset1_amount=Decimal(w['amount']), fee_amount=Decimal(w.get('transactionFee', '0')), fee_currency=w['asset']))

    def process_trades(self, data):
        for trade in data:
            # Адаптация под ответ API v1
            base_coin, quote_coin = trade['symbol'].split('-')
            self._add_transaction(dict(
                exchange_tx_id=f"bingx_trade_{trade['id']}", timestamp=_convert_bybit_timestamp(trade['time']), type='buy' if trade['side'] == 'BUY' else 'sell', raw_type=f"Spot Trade ({trade['side']})",
                asset1_ticker=base_coin, asset1_amount=Decimal(trade['qty']),
                asset2_ticker=quote_coin, asset2_amount=Decimal(trade['quoteQty']),
                execution_price=Decimal(trade['price']), fee_amount=Decimal(trade.get('commission', '0')), fee_currency=trade.get('commissionAsset')
            ))

class KucoinTransactionProcessor(BaseTransactionProcessor):
    def process_deposits(self, data):
        for d in data:
            if d.get('status') == 'SUCCESS':
                self._add_transaction(dict(exchange_tx_id=f"kucoin_deposit_{d['walletTxId']}", timestamp=_convert_bybit_timestamp(d['createdAt']), type='deposit', raw_type=f"Deposit (inner: {d.get('isInner', False)})", asset1_ticker=d['currency'], asset1_amount=Decimal(d['amount'])))

    def process_withdrawals(self, data):
        for w in data:
            if w.get('status') == 'SUCCESS':
                self._add_transaction(dict(exchange_tx_id=f"kucoin_withdrawal_{w['id']}", timestamp=_convert_bybit_timestamp(w['createdAt']), type='withdrawal', raw_type='Withdrawal', asset1_ticker=w['currency'], asset1_amount=Decimal(w['amount']), fee_amount=Decimal(w.get('fee', '0')), fee_currency=w['currency']))

    def process_trades(self, data):
        for trade in data:
            base_coin, quote_coin = trade['symbol'].split('-')
            self._add_transaction(dict(
                exchange_tx_id=f"kucoin_trade_{trade['tradeId']}", timestamp=_convert_bybit_timestamp(trade['createdAt']), type=trade['side'].lower(), raw_type=f"Spot Trade ({trade['side'].upper()})",
                asset1_ticker=base_coin, asset1_amount=Decimal(trade['size']),
                asset2_ticker=quote_coin, asset2_amount=Decimal(trade['funds']),
                execution_price=Decimal(trade['price']), fee_amount=Decimal(trade.get('fee', '0')), fee_currency=trade.get('feeCurrency')
            ))

    def process_transfers(self, data):
        for t in data:
            if t.get('direction', '').upper() != 'OUT': continue
            context = json.loads(t.get('context', '{}'))
            order_id = context.get('orderId')
            if not order_id: continue
            self._add_transaction(dict(exchange_tx_id=f"kucoin_transfer_{order_id}", timestamp=_convert_bybit_timestamp(t['createdAt']), type='transfer', raw_type=f"Transfer from {t.get('accountType', 'N/A')}", asset1_ticker=t['currency'], asset1_amount=Decimal(t['amount']), description=f"Internal transfer on {self.platform.name}"))

class OkxTransactionProcessor(BaseTransactionProcessor):
    def process_deposits(self, data):
        for d in data:
            if d.get('state') == '2':
                self._add_transaction(dict(exchange_tx_id=f"okx_deposit_{d['depId']}", timestamp=_convert_bybit_timestamp(d['ts']), type='deposit', raw_type='Deposit', asset1_ticker=d['ccy'], asset1_amount=Decimal(d['amt'])))

    def process_withdrawals(self, data):
        for w in data:
            if w.get('state') == '2':
                self._add_transaction(dict(exchange_tx_id=f"okx_withdrawal_{w['wdId']}", timestamp=_convert_bybit_timestamp(w['ts']), type='withdrawal', raw_type='Withdrawal', asset1_ticker=w['ccy'], asset1_amount=Decimal(w['amt']), fee_amount=Decimal(w.get('fee', '0')), fee_currency=w['ccy']))

    def process_trades(self, data):
        for trade in data:
            base_coin, quote_coin = trade['instId'].split('-')
            asset1_amount = Decimal(trade['fillSz'])
            self._add_transaction(dict(
                exchange_tx_id=f"okx_trade_{trade['tradeId']}", timestamp=_convert_bybit_timestamp(trade['ts']), type=trade['side'].lower(), raw_type=f"Spot Trade ({trade['side'].upper()})",
                asset1_ticker=base_coin, asset1_amount=asset1_amount,
                asset2_ticker=quote_coin, asset2_amount=asset1_amount * Decimal(trade['fillPx']),
                execution_price=Decimal(trade['fillPx']), fee_amount=abs(Decimal(trade.get('fee', '0'))), fee_currency=trade.get('feeCcy')
            ))

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

TRANSACTION_PROCESSOR_DISPATCHER = {
    'bybit': BybitTransactionProcessor,
    'bitget': BitgetTransactionProcessor,
    'bingx': BingxTransactionProcessor,
    'kucoin': KucoinTransactionProcessor,
    'okx': OkxTransactionProcessor,
}

from models import Transaction

# Maps a platform name to the function that fetches its market prices.
PRICE_TICKER_DISPATCHER = {
    'bybit': {'func': fetch_bybit_spot_tickers, 'suffix': 'USDT'},
    'bitget': {'func': fetch_bitget_spot_tickers, 'suffix': 'USDT'},
    'bingx': {'func': fetch_bingx_spot_tickers, 'suffix': '-USDT'},
    'kucoin': {'func': fetch_kucoin_spot_tickers, 'suffix': '-USDT'},
    'okx': {'func': fetch_okx_spot_tickers, 'suffix': '-USDT'},
}
