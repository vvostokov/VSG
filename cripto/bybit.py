import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from collections import defaultdict
import json # Убедитесь, что json импортирован

API_KEY = 'R7KlNAUMh75JAlpkpx'
API_SECRET = 'pRQTGlKGQAZSI1F1Dx6KfddW4sqMUfnqagHT'
BASE_URL = 'https://api.bybit.com'
RECV_WINDOW = '20000' # Увеличим окно до рекомендованного значения

def get_timestamp():
    """Возвращает текущий timestamp в миллисекундах в виде строки."""
    return str(int(time.time() * 1000))

def generate_signature(api_secret, api_key, timestamp, recv_window, params_for_signature_str):
    """
    Генерирует подпись для Bybit API v5 GET-запросов.
    params_for_signature_str - это уже сформированная строка query_string (для GET) или тело запроса (для POST).
    """
    # Формирование payload для подписи по документации Bybit V5:
    # timestamp + api_key + recv_window + params_for_signature_str
    payload = f"{timestamp}{api_key}{recv_window}{params_for_signature_str}"
    
    # Вычисление HMAC SHA256 и возвращение hex-строки
    sign = hmac.new(api_secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
    return sign

def api_get(path, params):
    """
    Выполняет GET-запрос к Bybit API V5 с автоматической генерацией подписи.
    """
    timestamp = get_timestamp()
    
    # Формируем query_string для URL и для подписи
    # Важно: recvWindow не должен быть частью query_string в URL, но должен быть в строке для подписи.
    # Однако, документация Bybit V5 для GET часто показывает recvWindow как часть query_string.
    # Давайте следовать примерам, где recvWindow включается в query_string.
    params_with_recv_window = params.copy()
    params_with_recv_window['recvWindow'] = RECV_WINDOW # Добавляем для URL
    query_string = urlencode(dict(sorted(params_with_recv_window.items()))) # Сортируем для консистентности

    sign = generate_signature(API_SECRET, API_KEY, timestamp, RECV_WINDOW, query_string) # generate_signature теперь возвращает только sign
    
    url = f"{BASE_URL}{path}?{query_string}"
    headers = {
        'X-BAPI-API-KEY': API_KEY,
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-RECV-WINDOW': RECV_WINDOW,
        'X-BAPI-SIGN': sign,
        'Content-Type': 'application/json'
    }

    # Отладочный вывод
    print(f"\n--- Запрос к: {path} ---")
    print(f"URL запроса: {url}")
    print("Заголовки запроса:")
    for k, v in headers.items():
        print(f"  {k}: {repr(v)}")

    try:
        response = requests.get(url, headers=headers, timeout=10) # Добавлен таймаут для устойчивости
        
        # Проверка на пустое тело ответа перед парсингом JSON
        if not response.text:
            print("Ответ сервера: (пустое тело ответа)")
            # Возвращаем кастомный код ошибки для пустых ответов
            return {"retCode": -1, "retMsg": "Пустое тело ответа от сервера"} 
        
        data_to_return = response.json()
        data_to_return['_request_path_debug'] = path # Добавляем путь для отладки в combine_balances
        print("JSON-ответ сервера:", json.dumps(data_to_return, indent=2))
        return data_to_return
    except requests.exceptions.RequestException as e:
        print(f"Ошибка сетевого запроса: {e}")
        return {"retCode": -1, "retMsg": f"Сетевая ошибка: {e}"}
    except json.JSONDecodeError as e:
        print(f"Ошибка при разборе JSON: {e}")
        print("Текст ответа сервера (не JSON):", response.text)
        return {"retCode": -1, "retMsg": f"Ошибка декодирования JSON: {e}"}

def get_unified_balances():
    """Получает балансы с Единого торгового аккаунта (Unified Trading Account)."""
    return api_get('/v5/account/wallet-balance', {'accountType': 'UNIFIED'})

def get_funding_balances():
    """Получает балансы с Аккаунта финансирования (Funding Account)."""
    # Используем рекомендованный эндпоинт для балансов аккаунта финансирования
    return api_get('/v5/asset/transfer/query-account-coins-balance', {'accountType': 'FUND'})

def get_earn_balances():
    """Получает балансы с продуктов Earn."""
    # Используем эндпоинт для получения активных позиций в Earn
    return api_get('/v5/asset/earn/position', {})

def combine_balances(*responses):
    """
    Объединяет балансы из разных ответов Bybit API, суммируя по каждой монете.
    """
    combined = defaultdict(float)
    for data in responses:
        if not data: # Пропускаем, если api_get вернул None
            continue
        # Пропускаем кастомные ошибки для пустых ответов или сетевых проблем
        if data.get('retCode') == -1: 
            print(f"Пропускаем данные из-за ошибки: {data.get('retMsg')}")
            continue

        if data.get('retCode') != 0:
            print(f"Ошибка API Bybit: {data}")
            continue
        
        result = data.get('result', {})
        
        coins_data_list = []
        path = data.get('_request_path_debug', '') # Предполагаем, что мы добавим это поле в api_get для отладки

        if path == '/v5/account/wallet-balance' and 'list' in result and result['list'] and 'coin' in result['list'][0]:
            coins_data_list = result['list'][0]['coin'] # Unified
        elif path == '/v5/asset/transfer/query-account-coins-balance' and 'balance' in result:
            coins_data_list = result['balance'] # Funding
        elif path == '/v5/asset/earn/position' and 'list' in result: # Earn - стандартный ответ для позиций
            coins_data_list = result['list']
        elif path == '/v5/asset/earn/position' and 'data' in result and 'list' in result['data']: # Альтернативная структура для Earn
            coins_data_list = result['data']['list']
        # Можно добавить сюда обработку для /v5/asset/coin-greeks, если он все же нужен для чего-то другого
        else:
            print(f"Неизвестная или пустая структура данных для суммирования из {path}: {result}")
            continue

        if not coins_data_list:
            continue

        for asset in coins_data_list:
            # Пытаемся получить тикер монеты из разных возможных полей
            coin = asset.get('coin') or asset.get('asset') 
            amount_str = "0"
            if path == '/v5/account/wallet-balance': # Unified
                amount_str = asset.get('walletBalance') or asset.get('equity') # walletBalance предпочтительнее
            elif path == '/v5/asset/transfer/query-account-coins-balance': # Funding
                amount_str = asset.get('walletBalance') # Используем walletBalance для Funding Account
            elif path == '/v5/asset/earn/position': # Earn
                # Для Earn позиций часто используются 'totalAmount', 'amount', или 'principalAmount'
                amount_str = asset.get('totalAmount') or asset.get('amount') or asset.get('principalAmount')

            try:
                amount = float(amount_str if amount_str is not None else 0)
            except (TypeError, ValueError):
                amount = 0 # В случае ошибки конвертации, считаем баланс нулевым
            
            if coin:
                combined[coin] += amount
    return combined

def main():
    """Основная функция для получения и вывода агрегированных балансов."""
    print("Получение данных с Unified Account...")
    unified = get_unified_balances()
    if unified.get("retCode") != 0:
        print(f"Ошибка при получении данных Unified Account: {unified.get('retMsg')}")

    print("\nПолучение данных с Funding Account...")
    funding = get_funding_balances()
    if funding.get("retCode") != 0:
        print(f"Ошибка при получении данных Funding Account: {funding.get('retMsg')}")

    print("\nПолучение данных с Earn Account...")
    earn = get_earn_balances()
    if earn.get("retCode") != 0: # Обрабатывает -1 для пустого ответа и другие ошибки API
        print(f"Ошибка или нет данных для Earn Account: {earn.get('retMsg')}")
    elif earn.get("retCode") == 0 and not earn.get("result", {}).get("list"):
        print("Данные для Earn Account получены успешно (retCode: 0), но список активов пуст или отсутствует.")

    combined = combine_balances(unified, funding, earn)

    print("\n--- Общий баланс по криптоактивам (Unified + Funding + Earn) ---")
    if not combined:
        print("Нет доступных балансов или все балансы нулевые.")
    else:
        # Сортируем для более читаемого вывода
        for coin, amount in sorted(combined.items()): 
            if amount > 0: # Выводим только ненулевые балансы
                print(f"{coin}: {amount}")

if __name__ == "__main__":
    main()
