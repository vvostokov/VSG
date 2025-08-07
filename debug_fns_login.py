import requests
import json
import getpass

# Этот скрипт предназначен для изолированной проверки входа в API ФНС.
# Он не является частью основного приложения.

# Константы, скопированные из fns_client.py
FNS_HOST = "https://irkkt-mobile.nalog.ru:8888"
LOGIN_ENDPOINT = "/v2/login"
HEADERS = {
    'Host': 'irkkt-mobile.nalog.ru:8888',
    'Accept': '*/*',
    'Device-OS': 'iOS',
    'Device-Id': '7C162434-14DE-448B-8524-420B404523A2',
    'clientVersion': '2.9.0',
    'Accept-Language': 'ru-RU,ru;q=0.9',
    'User-Agent': 'billchecker/2.9.0 (iPhone; iOS 13.6; Scale/2.00)',
    'Content-Type': 'application/json'
}

def test_fns_login(inn, password):
    """
    Тестирует вход в API ФНС с заданными ИНН и паролем.
    """
    print("--- Начало теста входа в ФНС ---")
    
    payload = {
        'inn': inn,
        'password': password,
        'client_secret': 'IyvrAbKt9h/8p6a7QPh8gpkXYQ4=',
        'os': 'iOS'
    }
    
    url = FNS_HOST + LOGIN_ENDPOINT
    
    print(f"Отправка POST-запроса на: {url}")
    print(f"Тело запроса (payload): {{'inn': '{inn}', 'password': '*****', ...}}")

    try:
        response = requests.post(url, headers=HEADERS, json=payload, timeout=20)
        
        print(f"\n--- Результат ---")
        print(f"Статус-код ответа: {response.status_code}")
        
        print("\nТело ответа:")
        print(response.text)

        if response.status_code == 200:
            print("\n[ВЫВОД]: УСПЕХ! Учетные данные верны.")
        else:
            print(f"\n[ВЫВОД]: ОШИБКА! Сервер отклонил учетные данные (Код: {response.status_code}).")
            print("Пожалуйста, еще раз перепроверьте ИНН и пароль на сайте lkfl2.nalog.ru.")

    except requests.exceptions.RequestException as e:
        print(f"\n--- СЕТЕВАЯ ОШИБКА ---")
        print(f"Не удалось подключиться к серверу ФНС: {e}")

if __name__ == '__main__':
    user_inn = input("Введите ваш ИНН (12 цифр): ").strip()
    user_password = getpass.getpass("Введите ваш пароль от личного кабинета ФНС: ").strip()
    
    if not user_inn or not user_password:
        print("ИНН и пароль не могут быть пустыми.")
    else:
        test_fns_login(user_inn, user_password)