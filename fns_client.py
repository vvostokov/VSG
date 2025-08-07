import os
import json
import time
from decimal import Decimal
from datetime import datetime
import requests

# --- Константы для API ФНС ---
FNS_HOST = "https://irkkt-mobile.nalog.ru:8888"
LOGIN_ENDPOINT = "/v2/login"
TICKET_ENDPOINT = "/v2/ticket"
TICKET_DETAILS_ENDPOINT = "/v2/tickets/{ticket_id}"

HEADERS = {
    'Host': 'irkkt-mobile.nalog.ru:8888',
    'Accept': '*/*',
    'Device-OS': 'iOS',
    'Device-Id': '7C162434-14DE-448B-8524-420B404523A2', # Может быть случайным UUID
    'clientVersion': '2.9.0',
    'Accept-Language': 'ru-RU,ru;q=0.9',
    'User-Agent': 'billchecker/2.9.0 (iPhone; iOS 13.6; Scale/2.00)',
}

# --- Глобальный кэш для клиента ФНС, чтобы не логиниться на каждый запрос ---
_fns_client_cache = None
_fns_client_cache_time = None
CACHE_TTL_SECONDS = 1800 # 30 минут


# --- Учетные данные из переменных окружения ---
FNS_API_USERNAME = os.environ.get('FNS_API_USERNAME') # ИНН (Идентификационный номер налогоплательщика)
FNS_API_PASSWORD = os.environ.get('FNS_API_PASSWORD') # Пароль от lkfl2.nalog.ru

class FNSClient:
    """Клиент для взаимодействия с API ФНС России."""
    def __init__(self, username, password):
        if not username or not password:
            raise ValueError("Необходимо передать ИНН и пароль для API ФНС.")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session_id = None

    def _login(self):
        """Выполняет вход в систему и получает ID сессии."""
        payload = {
            'inn': self.username,
            'password': self.password,
            'client_secret': 'IyvrAbKt9h/8p6a7QPh8gpkXYQ4=', # Статический ключ для мобильного приложения
            'os': 'iOS'
        }
        try:
            response = self.session.post(FNS_HOST + LOGIN_ENDPOINT, json=payload, timeout=10)
            response.raise_for_status()
            self.session_id = response.json().get('sessionId')
            self.session.headers['sessionId'] = self.session_id
        except requests.RequestException as e:
            raise Exception(f"Ошибка входа в ФНС. Проверьте учетные данные. Ответ сервера: {e.response.text if e.response else str(e)}")

    def get_receipt(self, qr_string: str) -> dict:
        """Получает детали чека по строке из QR-кода."""
        if not self.session_id:
            self._login()

        # 1. Получаем ID тикета по данным QR-кода
        try:
            response = self.session.post(FNS_HOST + TICKET_ENDPOINT, json={'qr': qr_string}, timeout=10)
            response.raise_for_status()
            ticket_id = response.json().get('id')
        except requests.RequestException as e:
            raise Exception(f"Ошибка получения ID тикета от ФНС: {e.response.text if e.response else str(e)}")

        # 2. Запрашиваем детали чека с попытками, так как он может обрабатываться
        receipt_url = FNS_HOST + TICKET_DETAILS_ENDPOINT.format(ticket_id=ticket_id)
        for _ in range(5): # Пытаемся 5 раз с задержкой
            try:
                response = self.session.get(receipt_url, timeout=10)
                if response.status_code == 200:
                    return response.json() # Успех
                elif response.status_code == 202: # Принято, но еще не обработано
                    time.sleep(2) # Ждем 2 секунды
                    continue
                response.raise_for_status()
            except requests.RequestException as e:
                raise Exception(f"Ошибка получения деталей чека: {e.response.text if e.response else str(e)}")
        
        raise Exception("Не удалось получить детали чека после нескольких попыток. Попробуйте позже.")

def get_fns_client():
    """
    Фабричная функция для получения кэшированного экземпляра FNSClient.
    Это позволяет избежать повторного входа в систему при каждом запросе.
    """
    global _fns_client_cache, _fns_client_cache_time

    now = time.time()
    # Сбрасываем кэш, если он устарел или пуст
    if not _fns_client_cache or not _fns_client_cache_time or (now - _fns_client_cache_time > CACHE_TTL_SECONDS):
        print("--- [FNS Client] Создание нового экземпляра клиента ФНС (кэш устарел или пуст).")
        if not FNS_API_USERNAME or not FNS_API_PASSWORD:
            raise ValueError("Учетные данные ФНС не настроены.")
        
        _fns_client_cache = FNSClient(FNS_API_USERNAME, FNS_API_PASSWORD)
        _fns_client_cache_time = now
    
    return _fns_client_cache

def parse_receipt_qr(qr_string: str) -> dict:
    """Парсит строку QR-кода с чека ФНС и возвращает структурированные данные."""
    if not FNS_API_USERNAME or not FNS_API_PASSWORD:
        raise ValueError("Необходимо задать переменные окружения FNS_API_USERNAME и FNS_API_PASSWORD.")
    if not qr_string or not qr_string.strip():
        raise ValueError("Строка QR-кода не может быть пустой.")
    try:
        # Используем фабричную функцию для получения клиента
        client = get_fns_client()
        receipt_json = client.get_receipt(qr_string)
        document_data = receipt_json.get('ticket', {}).get('document', {}).get('receipt', {})
        if not document_data:
            raise Exception("Ответ от ФНС не содержит данных о чеке.")
        parsed_data = {
            'date': datetime.fromtimestamp(document_data.get('dateTime')).isoformat() if document_data.get('dateTime') else None,
            'total_sum': Decimal(document_data.get('totalSum', 0)) / 100,
            'merchant': document_data.get('user'),
            'items': [],
            'error': None
        }
        for item in document_data.get('items', []):
            parsed_data['items'].append({
                'name': item.get('name'),
                'quantity': Decimal(str(item.get('quantity', 1))),
                'price': Decimal(item.get('price', 0)) / 100,
                'total': Decimal(item.get('sum', 0)) / 100,
            })
        return parsed_data
    except Exception as e:
        # Если произошла ошибка (особенно ошибка входа), сбрасываем кэш,
        # чтобы при следующей попытке был создан новый клиент.
        global _fns_client_cache
        _fns_client_cache = None
        print(f"Ошибка при обработке QR-кода: {e}")
        return {'error': str(e)}