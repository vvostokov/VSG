import time
import hmac
import hashlib
import base64
import requests
import json
from urllib.parse import urlencode
from collections import defaultdict
from datetime import datetime, timezone # Импортируем здесь для использования в функциях

# Ваши API ключи для OKX
API_KEY = '78ebc4ae-9ed2-44cc-a6e0-cbb3af9de33d'
API_SECRET = '412EB23611C64318753B3D1BCAADA523'
API_PASSPHRASE = 'The7Var90The7Var90!' # Ваша парольная фраза
BASE_URL = 'https://www.okx.com' # Базовый URL для OKX API v5

# Флаг для демо-торговли (0: реальная торговля, 1: демо-торговля)
# Установите '0' для работы с реальными средствами
SIMULATED_TRADING = '0' 

def get_timestamp_iso():
    """Возвращает текущий timestamp в формате ISO 8601 UTC."""
    # Используем datetime.now(timezone.utc) и корректно форматируем миллисекунды
    now_utc = datetime.now(timezone.utc)
    return now_utc.strftime('%Y-%m-%dT%H:%M:%S.') + now_utc.strftime('%f')[:3] + 'Z'

def generate_okx_signature(timestamp, method, request_path, body_str, api_secret):
    """
    Генерирует подпись для OKX API v5.
    request_path должен включать query string для GET запросов.
    body_str - это тело запроса в виде строки для POST/PUT, или пустая строка для GET.
    """
    message = timestamp + method.upper() + request_path + body_str
    mac = hmac.new(api_secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def api_request_okx(method, path, params=None, data_body=None):
    """
    Выполняет запрос к OKX API v5 с автоматической генерацией подписи.
    params: словарь query-параметров для GET-запросов.
    data_body: словарь для тела JSON POST/PUT-запросов.
    """
    method = method.upper()
    timestamp = get_timestamp_iso()
    
    query_string = ""
    if params:
        query_string = urlencode(sorted(params.items())) # OKX требует сортировки для консистентности, хотя не всегда для подписи
    
    request_path_for_signature = path
    if query_string:
        request_path_for_signature += "?" + query_string
        
    body_str_for_signature = ""
    if data_body:
        body_str_for_signature = json.dumps(data_body) # Тело запроса для подписи

    signature = generate_okx_signature(timestamp, method, request_path_for_signature, body_str_for_signature, API_SECRET)
    
    url = f"{BASE_URL}{request_path_for_signature}" # URL уже содержит query string, если есть
    
    headers = {
        'OK-ACCESS-KEY': API_KEY,
        'OK-ACCESS-SIGN': signature,
        'OK-ACCESS-TIMESTAMP': timestamp,
        'OK-ACCESS-PASSPHRASE': API_PASSPHRASE,
        'Content-Type': 'application/json',
        'x-simulated-trading': SIMULATED_TRADING 
    }

    print(f"\n--- Запрос OKX к: {path} ---")
    print(f"URL запроса: {url}")
    print("Заголовки запроса (OK-ACCESS-SIGN и OK-ACCESS-PASSPHRASE частично скрыты):")
    for k, v in headers.items():
        if k == 'OK-ACCESS-SIGN':
            print(f"  {k}: '{v[:5]}...'")
        elif k == 'OK-ACCESS-PASSPHRASE':
             print(f"  {k}: '{v[:3]}...{v[-3:]}'")
        else:
            print(f"  {k}: {repr(v)}")
    if data_body: print("Тело JSON запроса:", body_str_for_signature)


    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, timeout=20)
        elif method == 'POST':
            response = requests.post(url, headers=headers, data=body_str_for_signature, timeout=20)
        else:
            return {"code": "-1", "msg": f"Неподдерживаемый метод: {method}"} # OKX использует 'code' как строку

        if not response.text:
            print("Ответ сервера OKX: (пустое тело ответа)")
            return {"code": "-1", "msg": "Пустое тело ответа от сервера OKX"}
        
        response_data = response.json()
        response_data['_request_path_debug'] = path # Для отладки в combine_balances
        print("JSON-ответ сервера OKX:", json.dumps(response_data, indent=2))
        return response_data
    except requests.exceptions.RequestException as e:
        print(f"Ошибка сетевого запроса OKX: {e}")
        return {"code": "-1", "msg": f"Сетевая ошибка OKX: {e}"}
    except json.JSONDecodeError as e:
        print(f"Ошибка при разборе JSON от OKX: {e}")
        print("Текст ответа сервера OKX (не JSON):", response.text)
        return {"code": "-1", "msg": f"Ошибка декодирования JSON от OKX: {e}"}

# --- Функции для получения балансов с разных счетов OKX ---
# Названия эндпоинтов и структура ответов требуют проверки по документации OKX API v5!

def get_unified_account_balances_okx():
    """Получает балансы с единого счета OKX (включая спот, маржу, деривативы)."""
    # Эндпоинт для получения общего баланса аккаунта (может включать разные типы)
    path = '/api/v5/account/balance' 
    # Можно добавить параметр ccy для конкретных валют, например: params={'ccy': 'BTC,ETH,USDT'}
    return api_request_okx('GET', path)

def get_funding_account_balances_okx():
    """Получает балансы с финансового счета OKX (Funding Account)."""
    path = '/api/v5/asset/balances'
    # Можно добавить параметр ccy
    return api_request_okx('GET', path, params={'ccy': ''}) # Пустой ccy для всех валют на финансовом счете

def get_financial_account_balances_okx(): # Ранее Earn
    """Получает балансы с продуктов Grow/Financial (ранее Earn) OKX."""
    # Эндпоинт для продуктов Simple Earn (ранее Savings)
    # Для других продуктов Grow (Staking, DeFi и т.д.) могут быть другие эндпоинты
    path = '/api/v5/finance/savings/balance' 
    # Можно добавить параметр ccy
    return api_request_okx('GET', path)

# --- Объединение балансов ---
def combine_balances_okx(*responses):
    combined = defaultdict(float)
    for data in responses:
        if not data or data.get('code') != "0": # OKX использует 'code: "0"' для успеха
            error_msg = data.get('msg', 'Неизвестная ошибка или нет данных')
            req_path = data.get('_request_path_debug', 'неизвестного пути')
            print(f"Пропускаем данные из OKX для {req_path} из-за ошибки: {error_msg} (Код: {data.get('code')})")
            continue
        
        # Данные обычно в result.data (массив)
        # Структура ответа может отличаться для разных эндпоинтов
        
        path = data.get('_request_path_debug', '')
        assets_list = []

        if path == '/api/v5/account/balance': # Единый счет
            # Ответ: {"code":"0","data":[{"adjEq":"","details":[{"availBal":"","availEq":"","cashBal":"...","ccy":"USDT",...}],"imr":...}]}
            if data.get('data') and isinstance(data['data'], list) and len(data['data']) > 0:
                account_details = data['data'][0].get('details', [])
                for asset_info in account_details:
                    asset_name = asset_info.get('ccy')
                    # 'cashBal' - баланс актива, 'availBal' - доступный баланс
                    # Используем 'cashBal' как общий баланс для данного типа счета
                    amount_str = asset_info.get('cashBal') or asset_info.get('eq') # eq - стоимость в USD
                    if asset_name and amount_str:
                        try:
                            amount = float(amount_str)
                            if amount > 0:
                                combined[asset_name] += amount
                        except (ValueError, TypeError):
                            print(f"Не удалось сконвертировать баланс для {asset_name} в /api/v5/account/balance: {amount_str}")
        
        elif path == '/api/v5/asset/balances': # Финансовый счет
            # Ответ: {"code":"0","data":[{"availBal":"","bal":"","ccy":"USDT","frozenBal":""}],"msg":""}
            if data.get('data') and isinstance(data['data'], list):
                for asset_info in data['data']:
                    asset_name = asset_info.get('ccy')
                    # 'bal' - общий баланс, 'availBal' - доступный
                    amount_str = asset_info.get('bal') 
                    if asset_name and amount_str:
                        try:
                            amount = float(amount_str)
                            if amount > 0:
                                combined[asset_name] += amount
                        except (ValueError, TypeError):
                             print(f"Не удалось сконвертировать баланс для {asset_name} в /api/v5/asset/balances: {amount_str}")

        elif path == '/api/v5/finance/savings/balance': # Продукты Grow (Simple Earn/Savings)
            # Ответ: {"code":"0","data":[{"amt":"","ccy":"USDT","earnings":""}],"msg":""}
            if data.get('data') and isinstance(data['data'], list):
                for asset_info in data['data']:
                    asset_name = asset_info.get('ccy')
                    amount_str = asset_info.get('amt') # 'amt' - сумма в продукте
                    if asset_name and amount_str:
                        try:
                            amount = float(amount_str)
                            if amount > 0:
                                combined[asset_name] += amount
                        except (ValueError, TypeError):
                            print(f"Не удалось сконвертировать баланс для {asset_name} в /api/v5/finance/savings/balance: {amount_str}")
        else:
            print(f"Не удалось обработать структуру ответа OKX для {path}: {data.get('data')}")
            
    return combined

def main():
    print("Получение данных с OKX Unified Account...")
    unified_balances_data = get_unified_account_balances_okx()

    print("\nПолучение данных с OKX Funding Account...")
    funding_balances_data = get_funding_account_balances_okx()
    
    print("\nПолучение данных с OKX Financial (Grow/Savings) Account...")
    financial_balances_data = get_financial_account_balances_okx()

    combined = combine_balances_okx(unified_balances_data, funding_balances_data, financial_balances_data)

    print("\n--- Общий баланс по криптоактивам OKX (Unified + Funding + Financial) ---")
    if not combined:
        print("Нет доступных балансов или все балансы нулевые.")
    else:
        for coin, amount in sorted(combined.items()):
            if amount > 0: # Выводим только ненулевые балансы
                print(f"{coin}: {amount:.8f}") # Форматируем для криптовалют

if __name__ == "__main__":
    # datetime и timezone уже импортированы в начале файла
    main()
