import time
import hmac
import hashlib
import base64
import requests
from collections import defaultdict

API_KEY = 'bg_a724b51a56229f687a9f4712f7612cfe'
API_SECRET = 'd6cfbe5868f9343b237dc966047f95a2a725b98a61ae21bb9b88f420054d0d36'
PASSPHRASE = 'The7Var90The7Var90'
BASE_URL = 'https://api.bitget.com'

def get_timestamp():
    return str(int(time.time() * 1000))

def sign(message, secret):
    mac = hmac.new(secret.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def get_headers(method, path, body=''):
    timestamp = get_timestamp()
    method = method.upper()
    prehash = timestamp + method + path + body
    signature = sign(prehash, API_SECRET)
    return {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': signature,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type': 'application/json'
    }

def get_spot_balances():
    method = 'GET'
    path = '/api/v2/spot/account/assets'
    url = BASE_URL + path
    headers = get_headers(method, path)
    response = requests.get(url, headers=headers)
    data = response.json()
    if data.get('code') == '00000':
        return data.get('data', [])
    else:
        print(f"Ошибка получения спотовых балансов: {data}")
        return []

def get_earn_balances():
    method = 'GET'
    path = '/api/v2/earn/account/assets'
    url = BASE_URL + path
    headers = get_headers(method, path)
    response = requests.get(url, headers=headers)
    data = response.json()
    if data.get('code') == '00000':
        return data.get('data', [])
    else:
        print(f"Ошибка получения Earn балансов: {data}")
        return []

def combine_balances(spot_assets, earn_assets):
    combined = defaultdict(float)

    # Обработка спотовых балансов
    for asset in spot_assets:
        coin = asset.get('coin') or asset.get('currency') or asset.get('asset')
        available = asset.get('available') or asset.get('free') or 0
        frozen = asset.get('frozen') or asset.get('locked') or 0
        try:
            total = float(available) + float(frozen)
        except (TypeError, ValueError):
            total = 0
        if coin:
            combined[coin] += total

    # Обработка Earn балансов
    for asset in earn_assets:
        coin = asset.get('coin') or asset.get('currency') or asset.get('asset')
        amount = asset.get('amount') or asset.get('balance') or 0
        try:
            total = float(amount)
        except (TypeError, ValueError):
            total = 0
        if coin:
            combined[coin] += total

    return combined

def main():
    spot_assets = get_spot_balances()
    earn_assets = get_earn_balances()

    combined_balances = combine_balances(spot_assets, earn_assets)

    print("Общий баланс по криптоактивам (спот + Earn):")
    for coin, amount in combined_balances.items():
        print(f"{coin}: {amount}")

if __name__ == "__main__":
    main()
