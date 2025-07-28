import time
import base64
import hmac
import hashlib
import requests
import json

API_KEY = '68506aa9680d7c000102d224'
API_SECRET = 'eb3dad86-5060-4361-a3dc-3dc8e48235c7'
API_PASSPHRASE = 'The7Var90The7Var90' 
BASE_URL = 'https://api.kucoin.com'

def get_timestamp():
    # Время в миллисекундах в виде строки
    return str(int(time.time() * 1000))

def sign_request(method, endpoint, body, timestamp):
    """
    Формирует подпись для KuCoin API.
    """
    # Для GET-запросов с параметрами, они должны быть добавлены к endpoint после '?' и отсортированы
    str_to_sign = timestamp + method.upper() + endpoint + (body if body else '')
    # Используем API_SECRET напрямую, закодировав его в байты, а не декодируя из Base64
    hmac_key = API_SECRET.encode('utf-8')
    signature = hmac.new(hmac_key, str_to_sign.encode('utf-8'), hashlib.sha256)
    signature_b64 = base64.b64encode(signature.digest()).decode()
    return signature_b64

def get_headers(method, endpoint, body=''):
    timestamp = get_timestamp()
    signature = sign_request(method, endpoint, body, timestamp)
    passphrase = API_PASSPHRASE
    if passphrase:
        # Подпись passphrase тоже base64 HMAC SHA256
        # Используем API_SECRET как строку для ключа HMAC
        passphrase_signature = hmac.new(API_SECRET.encode('utf-8'), passphrase.encode('utf-8'), hashlib.sha256)
        passphrase = base64.b64encode(passphrase_signature.digest()).decode()
    headers = {
        'KC-API-KEY': API_KEY,
        'KC-API-SIGN': signature,
        'KC-API-TIMESTAMP': timestamp,
        'KC-API-PASSPHRASE': passphrase,
        'KC-API-KEY-VERSION': '2',
        'Content-Type': 'application/json'
    }
    return headers

def get_spot_balances():
    method = 'GET'
    endpoint = '/api/v1/accounts'
    body = ''
    url = BASE_URL + endpoint
    headers = get_headers(method, endpoint, body)

    response = requests.get(url, headers=headers)
    try:
        data = response.json()
    except Exception as e:
        print("Ошибка при разборе JSON:", e)
        print("Ответ сервера:", response.text)
        return None

    # Выводим полный ответ API для отладки
    print("\n--- Полный ответ API KuCoin (/api/v1/accounts) ---")
    print(json.dumps(data, indent=2))
    print("--------------------------------------------------")

    if data.get('code') == '200000':
        balances = data.get('data', [])
        combined = {}
        for asset in balances:
            coin = asset.get('currency')
            available = float(asset.get('available', 0))
            holds = float(asset.get('holds', 0)) # holds - это заблокированные средства
            total = available + holds
            if total > 0:
                combined[coin] = combined.get(coin, 0) + total # Суммируем балансы для одной монеты с разных счетов
        return combined
    else:
        print("Ошибка API KuCoin:", data)
        return None

def main():
    balances = get_spot_balances()
    if balances is not None: # Проверяем, что balances не None (в случае ошибки API)
        print("Баланс по криптоактивам KuCoin:")
        for coin, amount in sorted(balances.items()):
            print(f"{coin}: {amount}")

if __name__ == "__main__":
    main()
