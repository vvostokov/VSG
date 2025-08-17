from datetime import datetime, timezone
from decimal import Decimal
from extensions import db
from utils import encrypt_data, decrypt_data

class InvestmentPlatform(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    platform_type = db.Column(db.String(64), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    api_key = db.Column(db.String(256))
    api_secret_encrypted = db.Column(db.String(512))
    passphrase_encrypted = db.Column(db.String(512))
    other_credentials_json_encrypted = db.Column(db.Text)
    notes = db.Column(db.Text)
    last_sync_status = db.Column(db.String(128))
    last_synced_at = db.Column(db.DateTime)
    last_tx_synced_at = db.Column(db.DateTime) # Новая колонка для синхронизации транзакций
    manual_earn_balances_json = db.Column(db.Text, default='{}') # Новая колонка для ручных Earn балансов
    
    assets = db.relationship('InvestmentAsset', back_populates='platform', cascade="all, delete-orphan", lazy='dynamic')
    transactions = db.relationship('Transaction', back_populates='platform', cascade="all, delete-orphan", lazy='dynamic')

    @property
    def api_secret(self):
        return decrypt_data(self.api_secret_encrypted)

    @api_secret.setter
    def api_secret(self, value):
        self.api_secret_encrypted = encrypt_data(value)

    @property
    def passphrase(self):
        return decrypt_data(self.passphrase_encrypted)

    @passphrase.setter
    def passphrase(self, value):
        self.passphrase_encrypted = encrypt_data(value)

    @property
    def other_credentials_json(self):
        return decrypt_data(self.other_credentials_json_encrypted)

    @other_credentials_json.setter
    def other_credentials_json(self, value):
        self.other_credentials_json_encrypted = encrypt_data(value)

    def __repr__(self):
        return f'<InvestmentPlatform {self.name}>'
    @property
    def platform_type_display(self):
        types = {
            'crypto_exchange': 'Криптобиржа',
            'stock_broker': 'Брокер',
            'bank': 'Банк',
        }
        return types.get(self.platform_type, 'Другое')
class InvestmentAsset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticker = db.Column(db.String(32), nullable=False)
    name = db.Column(db.String(128))
    asset_type = db.Column(db.String(64))
    quantity = db.Column(db.Numeric(36, 18))
    current_price = db.Column(db.Numeric(20, 8))
    currency_of_price = db.Column(db.String(16))
    source_account_type = db.Column(db.String(100))
    platform_id = db.Column(db.Integer, db.ForeignKey('investment_platform.id'), nullable=False)
    platform = db.relationship('InvestmentPlatform', back_populates='assets')

    @property
    def asset_type_display(self):
        """Возвращает человекочитаемое название типа актива."""
        types = {
            'stock': 'Акция',
            'bond': 'Облигация',
            'etf': 'Фонд',
            'other': 'Другое',
        }
        return types.get(self.asset_type, self.asset_type.capitalize() if self.asset_type else 'Неизвестно')

    def __repr__(self):
        return f'<InvestmentAsset {self.ticker} on platform {self.platform_id}>'

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exchange_tx_id = db.Column(db.String(128), unique=True, nullable=True)
    timestamp = db.Column(db.DateTime, nullable=False, index=True)
    type = db.Column(db.String(64), nullable=False)
    raw_type = db.Column(db.String(128))
    asset1_ticker = db.Column(db.String(32))
    asset1_amount = db.Column(db.Numeric(36, 18))
    asset2_ticker = db.Column(db.String(32))
    asset2_amount = db.Column(db.Numeric(36, 18))
    fee_amount = db.Column(db.Numeric(36, 18))
    fee_currency = db.Column(db.String(32))
    execution_price = db.Column(db.Numeric(36, 18)) # Новое поле для цены исполнения сделки
    description = db.Column(db.Text)
    platform_id = db.Column(db.Integer, db.ForeignKey('investment_platform.id'), nullable=False)
    platform = db.relationship('InvestmentPlatform', back_populates='transactions')

    def __repr__(self):
        return f'<Transaction {self.id} on {self.timestamp}>'

class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    account_type = db.Column(db.String(64), nullable=False)
    currency = db.Column(db.String(16), nullable=False)
    balance = db.Column(db.Numeric(20, 2), nullable=False, default=0.0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text)
    # Поля для вкладов
    interest_rate = db.Column(db.Numeric(5, 2), nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    # Поля для кредитных карт
    credit_limit = db.Column(db.Numeric(20, 2), nullable=True)
    grace_period_days = db.Column(db.Integer, nullable=True)
    # Связь с банком
    bank_id = db.Column(db.Integer, db.ForeignKey('bank.id'), nullable=True)
    bank = db.relationship('Bank', back_populates='accounts')

    def __repr__(self):
        return f'<Account {self.name}>'

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    
    parent = db.relationship('Category', remote_side=[id], back_populates='subcategories')
    subcategories = db.relationship('Category', back_populates='parent', cascade="all, delete-orphan")
    
    __table_args__ = (db.UniqueConstraint('name', 'parent_id', 'type', name='_name_parent_type_uc'),)

    def __repr__(self):
        return f'<Category {self.name} ({self.type})>'

class Debt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    debt_type = db.Column(db.String(50), nullable=False)  # 'i_owe', 'owed_to_me'
    counterparty = db.Column(db.String(128), nullable=False)
    initial_amount = db.Column(db.Numeric(20, 2), nullable=False)
    repaid_amount = db.Column(db.Numeric(20, 2), nullable=False, default=0.0)
    currency = db.Column(db.String(16), nullable=False)
    status = db.Column(db.String(50), nullable=False, default='active') # 'active', 'repaid', 'cancelled'
    due_date = db.Column(db.Date, nullable=True)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f'<Debt {self.id} from/to {self.counterparty}>'

class BankingTransaction(db.Model):
    __tablename__ = 'banking_transaction'
    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Numeric(20, 2), nullable=False)
    to_amount = db.Column(db.Numeric(20, 2), nullable=True)
    transaction_type = db.Column(db.String(50), nullable=False)
    date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    description = db.Column(db.Text)
    merchant = db.Column(db.String(255), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    to_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    debt_id = db.Column(db.Integer, db.ForeignKey('debt.id'), nullable=True)
    account_ref = db.relationship('Account', foreign_keys=[account_id], backref=db.backref('transactions', lazy='dynamic'))
    to_account_ref = db.relationship('Account', foreign_keys=[to_account_id], backref=db.backref('incoming_transfers', lazy='dynamic'))
    category_ref = db.relationship('Category', backref=db.backref('transactions', lazy='dynamic'))
    debt_ref = db.relationship('Debt', backref=db.backref('repayments', lazy='dynamic'))
    
    # Связь с элементами транзакции (для покупок)
    items = db.relationship('TransactionItem', back_populates='transaction', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<BankingTransaction {self.id} {self.transaction_type} {self.amount}>'
class HistoricalPriceCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticker = db.Column(db.String(32), nullable=False, index=True)
    period = db.Column(db.String(10), nullable=False, index=True) # e.g., '7d', '30d'
    change_percent = db.Column(db.Float, nullable=True)
    last_updated = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint('ticker', 'period', name='_ticker_period_uc'),)

class PortfolioHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, unique=True, index=True)
    total_value_rub = db.Column(db.Numeric(20, 2), nullable=False)

class HistoricalPrice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticker = db.Column(db.String(32), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    price_usdt = db.Column(db.Numeric(20, 8), nullable=False)
    __table_args__ = (db.UniqueConstraint('ticker', 'date', name='_ticker_date_uc'),)

class SecuritiesPortfolioHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, unique=True, index=True)
    total_value_rub = db.Column(db.Numeric(20, 2), nullable=False)

class MoexHistoricalPrice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    isin = db.Column(db.String(32), nullable=False, index=True)
    date = db.Column(db.Date, nullable=False, index=True)
    price_rub = db.Column(db.Numeric(20, 8), nullable=False)
    __table_args__ = (db.UniqueConstraint('isin', 'date', name='_moex_isin_date_uc'),)

class JsonCache(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cache_key = db.Column(db.String(128), nullable=False, unique=True, index=True)
    json_data = db.Column(db.Text, nullable=False)
    last_updated = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f'<JsonCache {self.cache_key}>'

class TransactionItem(db.Model):
    __tablename__ = 'transaction_item'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(20, 3), nullable=False, default=1)
    price = db.Column(db.Numeric(20, 2), nullable=False)
    total = db.Column(db.Numeric(20, 2), nullable=False)
    
    transaction_id = db.Column(db.Integer, db.ForeignKey('banking_transaction.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    
    transaction = db.relationship('BankingTransaction', back_populates='items')
    category = db.relationship('Category')

    def __repr__(self):
        return f'<TransactionItem {self.name}>'

class Bank(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    accounts = db.relationship('Account', back_populates='bank', lazy='dynamic')

    def __repr__(self):
        return f'<Bank {self.name}>'
    
class TranslationCache(db.Model):
    """Кэш для хранения переводов текста."""
    __tablename__ = 'translation_cache'
    id = db.Column(db.Integer, primary_key=True)
    # Хэш используется как быстрый и уникальный ключ для оригинального текста
    source_hash = db.Column(db.String(32), nullable=False, index=True)
    source_lang = db.Column(db.String(10), nullable=False)
    target_lang = db.Column(db.String(10), nullable=False)
    translated_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Гарантируем, что для одной и той же фразы и пары языков будет только одна запись
    __table_args__ = (
        db.UniqueConstraint('source_hash', 'source_lang', 'target_lang', name='_source_hash_lang_uc'),
    )

    def __repr__(self):
        return f'<TranslationCache {self.source_hash} [{self.source_lang}->{self.target_lang}]>'
