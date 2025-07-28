from datetime import datetime, timezone
from decimal import Decimal
from extensions import db

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
    # Добавленные поля для вкладов и накопительных счетов
    interest_rate = db.Column(db.Numeric(5, 2), nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)

    def __repr__(self):
        return f'<Account {self.name}>'

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    type = db.Column(db.String(50), nullable=False)
    __table_args__ = (db.UniqueConstraint('name', 'type', name='_name_type_uc'),)

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
    transaction_type = db.Column(db.String(50), nullable=False)
    date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    description = db.Column(db.Text)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    to_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True)
    debt_id = db.Column(db.Integer, db.ForeignKey('debt.id'), nullable=True)
    account_ref = db.relationship('Account', foreign_keys=[account_id], backref=db.backref('transactions', lazy='dynamic'))
    to_account_ref = db.relationship('Account', foreign_keys=[to_account_id], backref=db.backref('incoming_transfers', lazy='dynamic'))
    category_ref = db.relationship('Category', backref=db.backref('transactions', lazy='dynamic'))
    debt_ref = db.relationship('Debt', backref=db.backref('repayments', lazy='dynamic'))

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