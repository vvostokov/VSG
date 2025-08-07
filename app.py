import os
from flask import Flask
from decimal import Decimal
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

from extensions import db, migrate

def create_app():
    """Application Factory."""
    # Загружаем переменные окружения из файла .env в самом начале
    load_dotenv()
    
    app = Flask(__name__)
    basedir = os.path.abspath(os.path.dirname(__file__))

    # --- Configuration ---
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'a-very-secret-key'
    app.config['SQLALCHEMY_DATABASE_URI'] = (
        os.environ.get('DATABASE_URL') or
        'sqlite:///' + os.path.join(basedir, 'app.db')
    )
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    app.config['ITEMS_PER_PAGE'] = 20
    # --- Инициализация Fernet для шифрования ---
    # Инициализируем Fernet для шифрования/дешифрования API-ключей.
    # Это нужно делать здесь, после load_dotenv(), чтобы гарантировать,
    # что используется правильный SECRET_KEY из .env файла.
    SALT = b'salt_for_zamliky_app' # Соль должна быть той же, что и при шифровании
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(app.config['SECRET_KEY'].encode()))
    app.config['FERNET'] = Fernet(key)
    # --- FNS API Credentials for QR Code parsing ---
    app.config['FNS_API_USERNAME'] = os.environ.get('FNS_API_USERNAME') # Ваш ИНН
    app.config['FNS_API_PASSWORD'] = os.environ.get('FNS_API_PASSWORD')

    # --- Initialize Extensions ---
    db.init_app(app)
    migrate.init_app(app, db)

    # --- Register Jinja Filters ---
    @app.template_filter()
    def trim_zeros(value):
        if isinstance(value, Decimal):
            value = value.normalize().to_eng_string()
        if isinstance(value, str) and '.' in value:
            return value.rstrip('0').rstrip('.')
        return value

    # --- Register Blueprints ---
    with app.app_context():
        # Import blueprints inside the context
        from main_routes import main_bp
        from api_routes import api_bp
        from commands import analytics_cli, seed_cli
        from securities_logic import securities_bp

        app.register_blueprint(main_bp)
        app.register_blueprint(securities_bp)
        app.register_blueprint(api_bp, url_prefix='/api')
        app.cli.add_command(analytics_cli)
        app.cli.add_command(seed_cli)

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(debug=True, host='0.0.0.0')
