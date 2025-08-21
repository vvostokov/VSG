import os
from flask import Flask
from decimal import Decimal
from datetime import datetime, timezone
from dotenv import load_dotenv
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

from extensions import db, migrate, scheduler

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
    # --- CryptoCompare News API Key ---
    app.config['CRYPTOCOMPARE_API_KEY'] = os.environ.get('CRYPTOCOMPARE_API_KEY')

    # --- Scheduler Configuration ---
    app.config['SCHEDULER_API_ENABLED'] = True
    app.config['JOBS'] = [
        {
            'id': 'job_update_news_cache',
            'func': 'background_tasks:update_all_news_in_background',
            'trigger': 'interval',
            'minutes': 30
        },
        {
            'id': 'job_sync_platforms',
            'func': 'background_tasks:sync_all_platforms_in_background',
            'trigger': 'interval',
            'hours': 2 # Синхронизировать балансы и транзакции каждые 2 часа
        },
        {
            'id': 'job_update_usdt_rub_rate',
            'func': 'background_tasks:update_usdt_rub_rate_in_background',
            'trigger': 'interval',
            'minutes': 15 # Обновлять курс каждые 15 минут
        }
    ]

    # --- Initialize Extensions ---
    db.init_app(app)
    migrate.init_app(app, db)
    scheduler.init_app(app)
    scheduler.start()

    # --- Register Jinja Filters ---
    @app.template_filter()
    def trim_zeros(value):
        if isinstance(value, Decimal):
            value = value.normalize().to_eng_string()
        if isinstance(value, str) and '.' in value:
            return value.rstrip('0').rstrip('.')
        return value

    @app.template_filter()
    def money_format(value, precision=2):
        """Форматирует число как денежную сумму с пробелами в качестве разделителей тысяч."""
        if value is None:
            return '-'
        try:
            return f"{Decimal(value):,.{precision}f}".replace(',', ' ')
        except (ValueError, TypeError):
            return str(value)
    @app.template_filter()
    def money_format(value, precision=2):
        """Форматирует число как денежную сумму с пробелами в качестве разделителей тысяч."""
        if value is None:
            return '-'
        try:
            return f"{Decimal(value):,.{precision}f}".replace(',', ' ')
        except (ValueError, TypeError):
            return str(value)
        
    @app.template_filter()
    def timestamp_to_datetime(ts):
        """Converts a UNIX timestamp to a timezone-aware datetime object."""
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (ValueError, TypeError):
            return None

    @app.template_filter()
    def datetime_format(dt, fmt='%d.%m.%Y %H:%M'):
        """Formats a datetime object into a string."""
        return dt.strftime(fmt) if dt else ''

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
