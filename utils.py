from flask import current_app

def encrypt_data(data: str) -> str:
    if not data:
        return ''
    # ИСПРАВЛЕНО: Получаем fernet из контекста приложения, чтобы гарантировать
    # использование правильного SECRET_KEY, загруженного в app factory.
    fernet = current_app.config['FERNET']
    return fernet.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    if not encrypted_data:
        return ''
    # ИСПРАВЛЕНО: Получаем fernet из контекста приложения.
    fernet = current_app.config['FERNET']
    return fernet.decrypt(encrypted_data.encode()).decode()