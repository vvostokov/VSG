import time
import hmac
import hashlib
import requests
import json
from urllib.parse import urlencode, quote_plus
from collections import defaultdict

# Ваши API ключи для BingX
API_KEY = 'VR5wuEf01Nw3LY1Jfg1d9nGat09DjQsKVR5tAeOhqAlvNS8tygfM4cqVoixevUDGs4Kd9OAnrHKzeIvHZw'
API_SECRET = 'nJlyVJO8mrGUe6zP26jgMc3uijRY8ZFhXq2h16MKWkKxB85aJucqF3UJP9NAexJnjl68m1bgiFMXfYdosQ'
BASE_URL = 'https://open-api.bingx.com' # Уточните базовый URL для API BingX

def get_timestamp_ms():
    """Возвращает текущий timestamp в миллисекундах в виде строки."""
    return str(int(time.time() * 1000))

def generate_bingx_signature(api_secret, params_string):
    """
    Генерирует подпись для BingX API.
    BingX обычно использует HMAC-SHA256. Строка для подписи - это query string.
    """
    # Точный метод формирования строки для подписи и сам алгоритм нужно проверить в документации BingX.
    # Обычно это query string (отсортированные параметры) или конкатенация параметров.
    # Для примера, предположим, что подписывается строка параметров.
    signature = hmac.new(api_secret.encode('utf-8'), params_string.encode('utf-8'), hashlib.sha256).hexdigest()
    return signature

def api_request_bingx(method, path, params=None, data=None):
    """
    Выполняет запрос к BingX API с автоматической генерацией подписи.
    """
    params = params or {}
    timestamp = get_timestamp_ms()
    
    # Параметры для подписи (без apiKey, так как он идет в заголовок)
    params_for_signing = {
        'timestamp': timestamp,
        **params  # Добавляем специфичные для эндпоинта параметры
    }
    
    # Параметры должны быть отсортированы по ключу для формирования строки подписи
    sorted_params = sorted(params_for_signing.items())
    params_string_for_signature = urlencode(sorted_params) # Строка для подписи
    
    signature = generate_bingx_signature(API_SECRET, params_string_for_signature)
    
    # Добавляем подпись к параметрам запроса
    # Имя параметра для подписи может быть 'sign' или 'signature' - уточнить в документации
    # BingX использует 'signature' в query string
    params_for_url = {**params_for_signing, 'signature': signature}
    
    query_string_for_url = urlencode(params_for_url)
    
    url = f"{BASE_URL}{path}?{query_string_for_url}"
    
    headers = {
        'X-BX-APIKEY': API_KEY,
        # Content-Type обычно нужен для POST/PUT, для GET можно убрать или оставить application/json
        # 'Content-Type': 'application/json' 
    }

    print(f"\n--- Запрос BingX к: {path} ---")
    print(f"URL запроса: {url.split('?')[0]}?timestamp=...&signature=...") # Маскируем часть URL
    # print(f"Полный URL (для отладки, содержит ключи!): {url}") 
    # print("Заголовки запроса:", headers)
    # if data: print("Тело POST запроса:", data)

    try:
        if method.upper() == 'GET':
            response = requests.get(url, headers=headers, timeout=20)
        elif method.upper() == 'POST':
            # Для POST BingX может ожидать параметры в теле x-www-form-urlencoded или JSON
            # Если x-www-form-urlencoded, то params_string_for_url можно передать в data
            # Если JSON, то data=json.dumps(params_for_signing) и Content-Type: application/json, а signature в URL
            # В данном примере, если POST, то параметры (включая signature) уже в URL, тело (data) может быть специфичным для эндпоинта
            response = requests.post(url, headers=headers, data=data, timeout=20) 
        else:
            return {"code": -1, "msg": f"Неподдерживаемый метод: {method}"}

        if not response.text:
            print("Ответ сервера BingX: (пустое тело ответа)")
            return {"code": -1, "msg": "Пустое тело ответа от сервера BingX"}
        
        response_data = response.json()
        response_data['_request_path_debug'] = path
        print("JSON-ответ сервера BingX:", json.dumps(response_data, indent=2))
        return response_data
    except requests.exceptions.RequestException as e:
        print(f"Ошибка сетевого запроса BingX: {e}")
        return {"code": -1, "msg": f"Сетевая ошибка BingX: {e}"}
    except json.JSONDecodeError as e:
        print(f"Ошибка при разборе JSON от BingX: {e}")
        print("Текст ответа сервера BingX (не JSON):", response.text)
        return {"code": -1, "msg": f"Ошибка декодирования JSON от BingX: {e}"}

# --- Функции для получения балансов с разных счетов BingX ---
# Названия эндпоинтов и структура ответов являются ПРЕДПОЛОЖЕНИЯМИ и требуют проверки по документации BingX!

def get_spot_balances_bingx():
    """Получает балансы со спотового счета BingX."""
    # Этот эндпоинт существует, но требует правильной аутентификации
    path = '/openApi/spot/v1/account/balance' 
    return api_request_bingx('GET', path)

# --- Объединение балансов ---
def combine_balances_bingx(*responses):
    combined = defaultdict(float)
    for data in responses:
        if not data or data.get('code') != 0: # BingX обычно использует 'code: 0' для успеха
            error_msg = data.get('msg', 'Неизвестная ошибка или нет данных')
            req_path = data.get('_request_path_debug', 'N/A')
            print(f"Пропускаем данные из BingX для {req_path} из-за ошибки: {error_msg} (Код: {data.get('code')})")
            continue
        
        result_data = data.get('data', {}) # Данные обычно в поле 'data'
        
        # Структура ответа может сильно отличаться. Это общие предположения.
        # 1. Для /openApi/spot/v1/account/balance
        if data.get('_request_path_debug') == '/openApi/spot/v1/account/balance' and 'balances' in result_data:
            for asset_info in result_data.get('balances', []):
                asset_name = asset_info.get('asset')
                # Суммируем 'free' и 'locked' для общего баланса
                free_amount = float(asset_info.get('free', 0))
                locked_amount = float(asset_info.get('locked', 0))
                total_amount = free_amount + locked_amount
                if asset_name and total_amount > 0:
                    combined[asset_name] += total_amount
        
        # 2. Для Funding/Общего кошелька (обновите '_request_path_debug' на актуальный путь, когда найдете)
        elif data.get('_request_path_debug') == 'АКТУАЛЬНЫЙ_ПУТЬ_ДЛЯ_FUNDING' and 'some_key' in result_data: # Замените 'АКТУАЛЬНЫЙ_ПУТЬ_ДЛЯ_FUNDING' и 'some_key'
             # Пример, если ответ это список балансов напрямую в result_data.balance
            if isinstance(result_data.get('balance'), list): # Если это список активов
                for asset_info in result_data.get('balance', []):
                    asset_name = asset_info.get('asset') or asset_info.get('currency')
                    # Поле для количества может называться 'balance', 'available', 'total'
                    amount_str = asset_info.get('balance') or asset_info.get('total') or asset_info.get('available') or "0"
                    try:
                        amount = float(amount_str)
                        if asset_name and amount > 0:
                            combined[asset_name] += amount
                    except (ValueError, TypeError):
                        pass
            # Если это словарь с одним балансом (маловероятно для общего)
            elif isinstance(result_data.get('balance'), dict) and 'asset' in result_data.get('balance'):
                asset_info = result_data.get('balance')
                asset_name = asset_info.get('asset')
                amount_str = asset_info.get('balance') or "0"
                try:
                    amount = float(amount_str)
                    if asset_name and amount > 0:
                        combined[asset_name] += amount
                except (ValueError, TypeError):
                    pass

        # 3. Для Earn (обновите '_request_path_debug' на актуальный путь, когда найдете)
        elif data.get('_request_path_debug') == 'АКТУАЛЬНЫЙ_ПУТЬ_ДЛЯ_EARN' and 'list' in result_data: # Замените 'АКТУАЛЬНЫЙ_ПУТЬ_ДЛЯ_EARN'
            for position in result_data.get('list', []):
                asset_name = position.get('asset') or position.get('coin')
                # Поле для количества может называться 'amount', 'totalAmount', 'principal'
                amount_str = position.get('amount') or position.get('totalAmount') or position.get('principal') or "0"
                try:
                    amount = float(amount_str)
                    if asset_name and amount > 0:
                        combined[asset_name] += amount
                except (ValueError, TypeError):
                    pass
        else:
            print(f"Не удалось обработать структуру ответа BingX для {data.get('_request_path_debug')}: {result_data}")

    return combined

def main():
    print("Получение данных с BingX Spot Account...")
    spot_balances = get_spot_balances_bingx()
    # Так как все данные приходят из spot_balances, передаем только его
    combined = combine_balances_bingx(spot_balances) 

    print("\n--- Общий баланс по криптоактивам BingX ---")
    if not combined:
        print("Нет доступных балансов или все балансы нулевые.")
    else:
        for coin, amount in sorted(combined.items()):
            if amount > 0:
                print(f"{coin}: {amount}")

if __name__ == "__main__":
    main()
