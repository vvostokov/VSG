import os
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

# Получаем ключ шифрования из переменной окружения.
# Это должен быть тот же SECRET_KEY, что и у Flask-приложения.
SECRET_KEY = os.environ.get('SECRET_KEY', 'a-default-secret-key-for-local-dev')

# Используем соль, чтобы сделать ключ более надежным. Можно оставить статичной.
SALT = b'salt_for_zamliky_app'

kdf = PBKDF2HMAC(
    algorithm=hashes.SHA256(),
    length=32,
    salt=SALT,
    iterations=100000,
)
key = base64.urlsafe_b64encode(kdf.derive(SECRET_KEY.encode()))
fernet = Fernet(key)

def encrypt_data(data: str) -> str:
    if not data:
        return ''
    return fernet.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data: str) -> str:
    if not encrypted_data:
        return ''
    return fernet.decrypt(encrypted_data.encode()).decode()