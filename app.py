from flask import Flask, request, jsonify, render_template, flash, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate 
import os 
from datetime import datetime, timezone 

# Инициализируем расширения глобально, но без привязки к приложению
db = SQLAlchemy()
migrate = Migrate()

# --- Модели ---
class Account(db.Model):
    __tablename__ = 'account'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    type = db.Column(db.String(50), nullable=False) # e.g., 'bank_account', 'cash', 'credit_card'
    balance = db.Column(db.Float, nullable=False, default=0.0)
    currency = db.Column(db.String(10), nullable=False, default='RUB')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    transactions = db.relationship('Transaction', backref='account_ref', lazy=True, foreign_keys='Transaction.account_id') # type: ignore
    cashbacks_received = db.relationship('Cashback', backref='account', lazy=True) # type: ignore
    # Связь для правил кэшбэка, где этот счет является целевым для зачисления
    cashback_rules_crediting_here = db.relationship('CashbackRule', backref='credit_to_account', lazy=True, foreign_keys='CashbackRule.credit_to_account_id') # type: ignore


    def __repr__(self):
        return f"<Account {self.name} ({self.balance} {self.currency})>"

class Category(db.Model):
    __tablename__ = 'category'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    type = db.Column(db.String(10), nullable=False) # 'income' or 'expense'

    transactions = db.relationship('Transaction', backref='category_ref', lazy=True) # type: ignore
    # Связь для правил кэшбэка, которые применяются к этой категории
    cashback_rules_applied_here = db.relationship('CashbackRule', backref='applies_to_category', lazy=True, foreign_keys='CashbackRule.applies_to_category_id') # type: ignore


    def __repr__(self):
        return f"<Category {self.name} ({self.type})>"

class Debt(db.Model): # type: ignore
     __tablename__ = 'debt'
    id = db.Column(db.Integer, primary_key=True)
    debt_type = db.Column(db.String(20), nullable=False)  # 'i_owe', 'owed_to_me'
    counterparty = db.Column(db.String(100), nullable=False)
    initial_amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), nullable=False, default='RUB')
    description = db.Column(db.String(255), nullable=True)
    due_date = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='active') # 'active', 'repaid', 'partially_repaid', 'cancelled'
    repaid_amount = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    payment_transactions = db.relationship('Transaction', backref='related_debt_ref', lazy='dynamic', foreign_keys='Transaction.related_debt_id') # type: ignore

    def __repr__(self):
        return f"<Debt {self.debt_type} to/from {self.counterparty} for {self.initial_amount} {self.currency}, Status: {self.status}>"

class Cashback(db.Model): # type: ignore
    __tablename__ = 'cashback'
    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), nullable=False, default='RUB')
    source = db.Column(db.String(100), nullable=True) # Может быть названием правила кэшбэка
    date_received = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    description = db.Column(db.String(255), nullable=True)
    
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    created_transaction_id = db.Column(db.Integer, db.ForeignKey('transaction.id'), nullable=True) # Транзакция дохода от кэшбэка
    
    created_transaction = db.relationship('Transaction', backref=db.backref('generated_cashback_entry', uselist=False), foreign_keys=[created_transaction_id]) # type: ignore
    original_expense_transaction_id = db.Column(db.Integer, db.ForeignKey('transaction.id'), nullable=True) # Ссылка на исходную расходную транзакцию (опционально)
    original_expense_transaction = db.relationship('Transaction', foreign_keys=[original_expense_transaction_id]) # type: ignore


    def __repr__(self):
        return f"<Cashback {self.amount} {self.currency} from {self.source} on {self.date_received}>"

class Transaction(db.Model): # type: ignore
    __tablename__ = 'transaction'
    id = db.Column(db.Integer, primary_key=True)
    amount = db.Column(db.Float, nullable=False)
    transaction_type = db.Column(db.String(30), nullable=False) # 'income', 'expense', 'transfer', 'debt_repayment_expense', 'debt_repayment_income'
    date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    description = db.Column(db.String(255), nullable=True)
    
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=True) 

    to_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=True)
    related_debt_id = db.Column(db.Integer, db.ForeignKey('debt.id'), nullable=True)
    
    # Backref 'generated_cashback_entry' from Cashback.created_transaction
    # No need to define 'generated_cashback' here if backref is set in Cashback

    def __repr__(self):
        return f"<Transaction {self.transaction_type} {self.amount} on {self.date}>"

# Новая модель для правил кэшбэка
class CashbackRule(db.Model): # type: ignore
    __tablename__ = 'cashback_rule'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    description = db.Column(db.String(255), nullable=True)
    cashback_percentage = db.Column(db.Float, nullable=False)  # e.g., 0.05 for 5%

    # Условие: к какой категории расходов применяется
    applies_to_category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    # applies_to_category - backref из Category.cashback_rules_applied_here

    # Действие: на какой счет зачислять кэшбэк
    credit_to_account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    # credit_to_account - backref из Account.cashback_rules_crediting_here

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<CashbackRule '{self.name}' ({self.cashback_percentage*100}% for category ID {self.applies_to_category_id} to account ID {self.credit_to_account_id})>"

# Фабрика приложений
def create_app_instance():
    _app = Flask(__name__)

    # Конфигурация базы данных
    # Для Render используется DATABASE_URL, для локальной разработки - sqlite
    db_uri = os.environ.get('DATABASE_URL') or 'sqlite:///finance_app.db'
    
    # SQLAlchemy предпочитает 'postgresql://' вместо 'postgres://' для psycopg2
    if db_uri and db_uri.startswith('postgres://'):
        db_uri = db_uri.replace('postgres://', 'postgresql://', 1)
        
    _app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
    _app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    # Для Render используется SECRET_KEY из переменных окружения, для локальной - заглушка
    _app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or 'your_local_fallback_secret_key_12345' 

    db.init_app(_app)
    migrate.init_app(_app, db)

    with _app.app_context():
        # Создаем таблицы, если они не существуют.
        # Это обходной путь для сред типа бесплатного тарифа Render без доступа к shell для `flask db upgrade`.
        # Для продакшн сред с доступом к shell, `flask db upgrade` является предпочтительным методом.
        # Примечание: db.create_all() не выполняет миграции схемы (например, добавление нового столбца в существующую таблицу).
        print(f"Attempting to call db.create_all(). Using DB URI: {_app.config['SQLALCHEMY_DATABASE_URI']}")
        db.create_all()
        print("db.create_all() executed.")

    return _app

app = create_app_instance() # Создаем экземпляр приложения для использования декораторами @app.route

# --- Маршруты для UI (пользовательского интерфейса) ---

@app.route('/')
def index():
    total_balance_data = get_total_balance_data()
    recent_transactions = Transaction.query.order_by(Transaction.date.desc()).limit(5).all()
    
    # Получаем сводку по кэшбэку
    cashback_summary_raw = db.session.query(
        Cashback.currency,
        db.func.sum(Cashback.amount).label('total_cashback')
    ).group_by(Cashback.currency).all()
    
    total_cashback_by_currency = {currency: round(total, 2) for currency, total in cashback_summary_raw}

    return render_template('index.html',
                           total_balance_by_currency=total_balance_data,
                           recent_transactions=recent_transactions,
                           total_cashback_by_currency=total_cashback_by_currency)
 
def get_total_balance_data():
    total_balance_by_currency = {}
    accounts_db = Account.query.all()
    if not accounts_db:
        return None
    for acc in accounts_db:
        if acc.currency in total_balance_by_currency:
            total_balance_by_currency[acc.currency] += acc.balance
        else:
            total_balance_by_currency[acc.currency] = acc.balance
    for currency_key in total_balance_by_currency:
        total_balance_by_currency[currency_key] = round(total_balance_by_currency[currency_key], 2)
    return total_balance_by_currency

@app.route('/ui/accounts')
def ui_accounts():
    accounts_data = Account.query.order_by(Account.name).all()
    return render_template('accounts.html', accounts=accounts_data)

@app.route('/ui/add-account', methods=['GET', 'POST'])
def ui_add_account_form():
    if request.method == 'POST':
        name = request.form.get('name')
        acc_type = request.form.get('type')
        balance_str = request.form.get('balance', '0.0')
        currency = request.form.get('currency', 'RUB')

        if not name or not acc_type:
            flash('Название и тип счета обязательны.', 'danger')
            return redirect(url_for('ui_add_account_form'))
        
        try:
            balance = float(balance_str)
        except ValueError:
            flash('Некорректное значение баланса.', 'danger')
            return redirect(url_for('ui_add_account_form'))

        existing_account = Account.query.filter_by(name=name).first()
        if existing_account:
            flash(f'Счет с названием "{name}" уже существует.', 'warning')
            return redirect(url_for('ui_add_account_form'))

        new_account = Account(name=name, type=acc_type, balance=balance, currency=currency)
        db.session.add(new_account)
        db.session.commit()
        flash(f'Счет "{name}" успешно создан!', 'success')
        return redirect(url_for('ui_accounts'))
        
    return render_template('add_account.html')

@app.route('/ui/accounts/<int:account_id>/edit', methods=['GET', 'POST'])
def ui_edit_account_form(account_id):
    account = Account.query.get_or_404(account_id)
    if request.method == 'POST':
        new_name = request.form.get('name')
        new_type = request.form.get('type')
        new_balance_str = request.form.get('balance')
        new_currency = request.form.get('currency')

        if not all([new_name, new_type, new_balance_str, new_currency]):
            flash('Все поля обязательны для заполнения.', 'danger')
            return render_template('edit_account.html', account=account)

        try:
            new_balance = float(new_balance_str)
        except ValueError:
            flash('Некорректное значение баланса.', 'danger')
            return render_template('edit_account.html', account=account)

        # Проверка, если имя изменилось и новое имя уже занято другим счетом
        if new_name != account.name:
            existing_account_with_new_name = Account.query.filter(Account.name == new_name, Account.id != account_id).first()
            if existing_account_with_new_name:
                flash(f'Счет с названием "{new_name}" уже существует.', 'warning')
                return render_template('edit_account.html', account=account)
        
        account.name = new_name
        account.type = new_type
        account.balance = new_balance # Прямое изменение баланса
        account.currency = new_currency
        
        db.session.commit()
        flash(f'Счет "{account.name}" успешно обновлен!', 'success')
        return redirect(url_for('ui_accounts'))

    return render_template('edit_account.html', account=account)

@app.route('/ui/accounts/<int:account_id>/delete', methods=['POST'])
def ui_delete_account(account_id):
    account_to_delete = Account.query.get_or_404(account_id)
    
    # Проверка на связанные транзакции
    if Transaction.query.filter((Transaction.account_id == account_id) | (Transaction.to_account_id == account_id)).first():
        flash(f'Нельзя удалить счет "{account_to_delete.name}", так как с ним связаны транзакции.', 'danger')
        return redirect(url_for('ui_accounts'))
    
    # Проверка на связанные кэшбэки (зачисление)
    if Cashback.query.filter_by(account_id=account_id).first():
        flash(f'Нельзя удалить счет "{account_to_delete.name}", так как на него зачислены кэшбэки.', 'danger')
        return redirect(url_for('ui_accounts'))

    # Проверка на использование в правилах кэшбэка
    if CashbackRule.query.filter_by(credit_to_account_id=account_id).first():
        flash(f'Нельзя удалить счет "{account_to_delete.name}", так как он используется в правилах кэшбэка для зачисления.', 'danger')
        return redirect(url_for('ui_accounts'))

    db.session.delete(account_to_delete)
    db.session.commit()
    flash(f'Счет "{account_to_delete.name}" успешно удален.', 'success')
    return redirect(url_for('ui_accounts'))

@app.route('/ui/transactions')
def ui_transactions():
    page = request.args.get('page', 1, type=int)
    per_page = 15
    
    account_id_filter = request.args.get('account_id', type=int)
    category_id_filter = request.args.get('category_id', type=int)
    
    query = Transaction.query.order_by(Transaction.date.desc())
    
    if account_id_filter:
        query = query.filter_by(account_id=account_id_filter)
    if category_id_filter:
        query = query.filter_by(category_id=category_id_filter)

    transactions_pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    transactions_data = transactions_pagination.items
    
    accounts = Account.query.order_by(Account.name).all()
    categories = Category.query.order_by(Category.name).all()

    return render_template('transactions.html', 
                           transactions=transactions_data,
                           pagination=transactions_pagination,
                           accounts=accounts,
                           categories=categories,
                           selected_account_id=account_id_filter,
                           selected_category_id=category_id_filter,
                           Account=Account # Pass the Account model for querying in template
                           )

def _apply_cashback_rules(original_transaction: Transaction):
    """
    Проверяет и применяет правила кэшбэка к расходной транзакции.
    Создает доходную транзакцию для кэшбэка и запись в Cashback.
    """
    if original_transaction.transaction_type != 'expense' or not original_transaction.category_id:
        return

    rules = CashbackRule.query.filter_by(
        applies_to_category_id=original_transaction.category_id,
        is_active=True
    ).all()

    for rule in rules:
        cashback_amount = round(original_transaction.amount * rule.cashback_percentage, 2)
        if cashback_amount <= 0:
            continue

        target_account = Account.query.get(rule.credit_to_account_id)
        if not target_account:
            flash(f"Ошибка кэшбэка: Целевой счет для правила '{rule.name}' не найден.", "danger")
            continue
        
        # Проверка совпадения валют (или нужна конвертация?)
        # Для простоты пока предполагаем, что валюта кэшбэка совпадает с валютой счета зачисления
        # и валютой исходной транзакции. В будущем можно добавить конвертацию.
        if target_account.currency != original_transaction.account_ref.currency:
            flash(f"Предупреждение: Валюта счета для зачисления кэшбэка ({target_account.currency}) по правилу '{rule.name}' "
                  f"не совпадает с валютой исходной транзакции ({original_transaction.account_ref.currency}). Кэшбэк не начислен.", "warning")
            continue


        cashback_category_name = "Кэшбэк" # Стандартное имя для категории дохода от кэшбэка
        income_category = Category.query.filter_by(name=cashback_category_name, type='income').first()
        if not income_category:
            income_category = Category(name=cashback_category_name, type='income')
            db.session.add(income_category)
            # db.session.flush() # чтобы получить ID, если он нужен сразу

        cashback_description = f"Кэшбэк по правилу '{rule.name}' (транзакция #{original_transaction.id})"
        
        # Создаем доходную транзакцию для кэшбэка
        cashback_income_transaction = Transaction(
            amount=cashback_amount,
            transaction_type='income',
            date=original_transaction.date, # Дата кэшбэка = дата исходной транзакции
            description=cashback_description,
            account_id=target_account.id,
            category_id=income_category.id
        )
        db.session.add(cashback_income_transaction)
        target_account.balance += cashback_amount
        db.session.flush() # Чтобы получить ID для cashback_income_transaction.id

        # Создаем запись в таблице Cashback
        new_cashback_entry = Cashback(
            amount=cashback_amount,
            currency=target_account.currency, # Валюта счета зачисления
            source=rule.name,
            date_received=cashback_income_transaction.date,
            description=cashback_description,
            account_id=target_account.id,
            created_transaction_id=cashback_income_transaction.id,
            original_expense_transaction_id=original_transaction.id
        )
        db.session.add(new_cashback_entry)
        
        flash(f"Начислен кэшбэк {cashback_amount} {target_account.currency} по правилу '{rule.name}' на счет '{target_account.name}'.", "info")


@app.route('/ui/add-transaction', methods=['GET', 'POST'])
def ui_add_transaction_form():
    if request.method == 'POST':
        amount_str = request.form.get('amount')
        transaction_type = request.form.get('transaction_type')
        account_id = request.form.get('account_id', type=int)
        category_id_str = request.form.get('category_id') # Get as string first
        date_str = request.form.get('date')
        description = request.form.get('description')
        to_account_id_str = request.form.get('to_account_id') # Get as string first

        category_id = int(category_id_str) if category_id_str and category_id_str.isdigit() else None
        to_account_id = int(to_account_id_str) if to_account_id_str and to_account_id_str.isdigit() else None


        if not all([amount_str, transaction_type, account_id, date_str]):
            flash('Заполните все обязательные поля: Сумма, Тип, Счет, Дата.', 'danger')
            return redirect(url_for('ui_add_transaction_form'))
        
        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError("Amount must be positive")
        except ValueError:
            flash('Некорректная сумма.', 'danger')
            return redirect(url_for('ui_add_transaction_form'))

        account = Account.query.get(account_id)
        if not account:
            flash('Выбранный счет не найден.', 'danger')
            return redirect(url_for('ui_add_transaction_form'))

        category = None
        if category_id:
            category = Category.query.get(category_id)
            if not category and transaction_type != 'transfer': # Для переводов категория не обязательна
                flash('Выбранная категория не найдена.', 'danger')
                return redirect(url_for('ui_add_transaction_form'))
        
        if category and transaction_type == 'income' and category.type != 'income':
            flash(f'Категория "{category.name}" не является доходной.', 'danger')
            return redirect(url_for('ui_add_transaction_form'))
        if category and transaction_type == 'expense' and category.type != 'expense':
            flash(f'Категория "{category.name}" не является расходной.', 'danger')
            return redirect(url_for('ui_add_transaction_form'))
        
        # Категория обязательна для дохода/расхода (кроме случаев, когда это кэшбэк-транзакция, но это обрабатывается отдельно)
        if transaction_type in ['income', 'expense'] and not category_id:
            flash('Категория обязательна для транзакций типа "доход" или "расход".', 'danger')
            return redirect(url_for('ui_add_transaction_form'))


        try:
            transaction_date = datetime.strptime(date_str, '%Y-%m-%d')
            # Для корректной работы с timezone-aware datetime в базе, если используется
            # transaction_date = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc) 
        except ValueError:
            flash('Некорректный формат даты. Используйте ГГГГ-ММ-ДД.', 'danger')
            return redirect(url_for('ui_add_transaction_form'))

        new_transaction = Transaction(
            amount=amount,
            transaction_type=transaction_type,
            date=transaction_date,
            description=description,
            account_id=account.id,
            category_id=category.id if category and transaction_type != 'transfer' else None
        )

        if transaction_type == 'income':
            account.balance += amount
        elif transaction_type == 'expense':
            if account.balance < amount and account.type != 'credit_card': # Кредитки могут уходить в минус
                flash('Недостаточно средств на счете.', 'warning')
                # return redirect(url_for('ui_add_transaction_form')) # Можно раскомментировать, если строго запрещать
            account.balance -= amount
        elif transaction_type == 'transfer':
            if not to_account_id:
                flash('Для перевода необходимо указать счет назначения.', 'danger')
                return redirect(url_for('ui_add_transaction_form'))
            if to_account_id == account.id:
                flash('Нельзя перевести средства на тот же счет.', 'danger')
                return redirect(url_for('ui_add_transaction_form'))
            
            to_account = Account.query.get(to_account_id)
            if not to_account:
                flash('Счет назначения не найден.', 'danger')
                return redirect(url_for('ui_add_transaction_form'))
            
            if account.currency != to_account.currency:
                flash(f'Переводы между счетами с разной валютой ({account.currency} -> {to_account.currency}) пока не поддерживаются автоматически. Пожалуйста, выполните две отдельные транзакции (расход и доход) с конвертацией вручную.', 'warning')
                return redirect(url_for('ui_add_transaction_form'))

            if account.balance < amount and account.type != 'credit_card':
                flash('Недостаточно средств для перевода.', 'warning')
                # return redirect(url_for('ui_add_transaction_form'))
            
            account.balance -= amount
            to_account.balance += amount
            new_transaction.to_account_id = to_account_id
            new_transaction.category_id = None # У переводов нет категории
        
        db.session.add(new_transaction)
        
        # Применение правил кэшбэка после добавления основной транзакции
        if new_transaction.transaction_type == 'expense':
            db.session.flush() # Чтобы new_transaction получил ID и account_ref был доступен
            _apply_cashback_rules(new_transaction)

        db.session.commit()
        flash('Транзакция успешно добавлена!', 'success')
        return redirect(url_for('ui_transactions'))

    accounts_data = Account.query.order_by(Account.name).all()
    # Для формы добавления транзакции нужны все категории, JS будет фильтровать
    categories_data = Category.query.order_by(Category.name).all() 
    return render_template('add_transaction.html', accounts=accounts_data, categories=categories_data, now=datetime.now(timezone.utc))

@app.route('/ui/transactions/<int:transaction_id>/edit', methods=['GET', 'POST'])
def ui_edit_transaction_form(transaction_id):
    transaction_to_edit = Transaction.query.get_or_404(transaction_id)
    # TODO: Implement GET to show form pre-filled with transaction_to_edit data
    # TODO: Implement POST to update transaction and account balances, handle cashback
    if request.method == 'POST':
        # Logic for updating the transaction will go here
        flash('Функция редактирования транзакции в разработке.', 'info')
        return redirect(url_for('ui_transactions'))

    accounts_data = Account.query.order_by(Account.name).all()
    categories_data = Category.query.order_by(Category.name).all()
    return render_template('edit_transaction.html', transaction=transaction_to_edit, accounts=accounts_data, categories=categories_data)

@app.route('/ui/transactions/<int:transaction_id>/delete', methods=['POST'])
def ui_delete_transaction(transaction_id):
    transaction_to_delete = Transaction.query.get_or_404(transaction_id)
    account = transaction_to_delete.account_ref
    to_account = Account.query.get(transaction_to_delete.to_account_id) if transaction_to_delete.to_account_id else None

    try:
        # 1. Откатываем изменения балансов
        if transaction_to_delete.transaction_type == 'income':
            if account:
                account.balance -= transaction_to_delete.amount
        elif transaction_to_delete.transaction_type == 'expense':
            if account:
                account.balance += transaction_to_delete.amount
        elif transaction_to_delete.transaction_type == 'transfer':
            if account:
                account.balance += transaction_to_delete.amount
            if to_account:
                to_account.balance -= transaction_to_delete.amount
        elif transaction_to_delete.transaction_type in ['debt_repayment_expense', 'debt_repayment_income']:
            if transaction_to_delete.related_debt_id:
                debt = Debt.query.get(transaction_to_delete.related_debt_id)
                if debt:
                    debt.repaid_amount -= transaction_to_delete.amount
                    if debt.repaid_amount < 0: debt.repaid_amount = 0 # Предохранитель
                    
                    if abs(debt.repaid_amount - debt.initial_amount) < 0.001:
                        debt.status = 'repaid'
                    elif debt.repaid_amount > 0:
                        debt.status = 'partially_repaid'
                    else:
                        debt.status = 'active'
                    
                    # Корректируем баланс счета, связанного с погашением долга
                    if transaction_to_delete.transaction_type == 'debt_repayment_expense':
                        if account: account.balance += transaction_to_delete.amount # Возвращаем деньги на счет
                    elif transaction_to_delete.transaction_type == 'debt_repayment_income':
                        if account: account.balance -= transaction_to_delete.amount # Забираем деньги со счета

        # 2. Обрабатываем связанный кэшбэк
        # Если удаляемая транзакция - это расход, который породил кэшбэк
        cashback_entry_from_this_expense = Cashback.query.filter_by(original_expense_transaction_id=transaction_to_delete.id).first()
        if cashback_entry_from_this_expense:
            cashback_income_transaction = cashback_entry_from_this_expense.created_transaction
            if cashback_income_transaction:
                cashback_account = cashback_income_transaction.account_ref
                if cashback_account:
                    cashback_account.balance -= cashback_income_transaction.amount
                db.session.delete(cashback_income_transaction)
            db.session.delete(cashback_entry_from_this_expense)

        # Если удаляемая транзакция - это доход от кэшбэка
        if transaction_to_delete.generated_cashback_entry:
            # Баланс уже скорректирован выше (как обычный доход), просто удаляем запись Cashback
            db.session.delete(transaction_to_delete.generated_cashback_entry)

        # 3. Удаляем саму транзакцию
        db.session.delete(transaction_to_delete)
        db.session.commit()
        flash(f'Транзакция #{transaction_to_delete.id} ({transaction_to_delete.description or "Без описания"}) успешно удалена.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при удалении транзакции: {str(e)}', 'danger')
        
    return redirect(url_for('ui_transactions'))

@app.route('/ui/debts')
def ui_debts():фв
    debts_data = Debt.query.order_by(Debt.created_at.desc()).all()
    return render_template('debts.html', debts=debts_data)

@app.route('/ui/add-debt', methods=['GET', 'POST'])
def ui_add_debt_form():
    if request.method == 'POST':
        debt_type = request.form.get('debt_type')
        counterparty = request.form.get('counterparty')
        initial_amount_str = request.form.get('initial_amount')
        currency = request.form.get('currency', 'RUB')
        description = request.form.get('description')
        due_date_str = request.form.get('due_date')

        if not all([debt_type, counterparty, initial_amount_str]):
            flash('Тип долга, контрагент и сумма обязательны.', 'danger')
            return redirect(url_for('ui_add_debt_form'))
        
        try:
            initial_amount = float(initial_amount_str)
            if initial_amount <= 0:
                raise ValueError
        except ValueError:
            flash('Некорректная сумма долга.', 'danger')
            return redirect(url_for('ui_add_debt_form'))

        due_date = None
        if due_date_str:
            try:
                due_date = datetime.strptime(due_date_str, '%Y-%m-%d')
            except ValueError:
                flash('Некорректный формат даты возврата.', 'danger')
                return redirect(url_for('ui_add_debt_form'))
        
        new_debt = Debt(
            debt_type=debt_type,
            counterparty=counterparty,
            initial_amount=initial_amount,
            currency=currency,
            description=description,
            due_date=due_date,
            status='active'
        )
        db.session.add(new_debt)
        db.session.commit()
        flash('Долг успешно добавлен!', 'success')
        return redirect(url_for('ui_debts'))

    return render_template('add_debt.html')

@app.route('/ui/debts/<int:debt_id>/repay', methods=['GET', 'POST'])
def ui_repay_debt_form(debt_id):
    debt = Debt.query.get_or_404(debt_id)
    if debt.status == 'repaid':
        flash('Этот долг уже полностью погашен.', 'info')
        return redirect(url_for('ui_debts'))
    if debt.status == 'cancelled':
        flash('Нельзя погасить аннулированный долг.', 'warning')
        return redirect(url_for('ui_debts'))

    remaining_debt = debt.initial_amount - debt.repaid_amount

    if request.method == 'POST':
        amount_str = request.form.get('amount')
        account_id = request.form.get('account_id', type=int)
        date_str = request.form.get('date')
        description = request.form.get('description')

        if not all([amount_str, account_id, date_str]):
            flash('Сумма, счет и дата обязательны для погашения.', 'danger')
            return redirect(url_for('ui_repay_debt_form', debt_id=debt_id))

        try:
            repayment_amount = float(amount_str)
            if repayment_amount <= 0:
                flash('Сумма погашения должна быть положительной.', 'danger')
                return redirect(url_for('ui_repay_debt_form', debt_id=debt_id))
            if repayment_amount > remaining_debt:
                flash(f'Сумма погашения {repayment_amount} превышает остаток по долгу {remaining_debt}. Погашено будет {remaining_debt}.', 'warning')
                repayment_amount = remaining_debt
        except ValueError:
            flash('Некорректная сумма погашения.', 'danger')
            return redirect(url_for('ui_repay_debt_form', debt_id=debt_id))

        account = Account.query.get(account_id)
        if not account:
            flash('Счет для операции не найден.', 'danger')
            return redirect(url_for('ui_repay_debt_form', debt_id=debt_id))
        
        if account.currency != debt.currency:
            flash(f'Валюта счета ({account.currency}) не совпадает с валютой долга ({debt.currency}). Операция отменена.', 'danger')
            return redirect(url_for('ui_repay_debt_form', debt_id=debt_id))

        try:
            repayment_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            flash('Некорректный формат даты.', 'danger')
            return redirect(url_for('ui_repay_debt_form', debt_id=debt_id))

        transaction_type_for_repayment = None
        final_description = description
        
        if debt.debt_type == 'i_owe':
            transaction_type_for_repayment = 'debt_repayment_expense'
            if account.balance < repayment_amount and account.type != 'credit_card':
                flash('Недостаточно средств на счете для погашения.', 'warning')
                return redirect(url_for('ui_repay_debt_form', debt_id=debt_id))
            account.balance -= repayment_amount
            if not final_description: final_description = f"Погашение долга: {debt.counterparty}"
        
        elif debt.debt_type == 'owed_to_me':
            transaction_type_for_repayment = 'debt_repayment_income'
            account.balance += repayment_amount
            if not final_description: final_description = f"Получение по долгу от: {debt.counterparty}"

        new_repayment_transaction = Transaction(
            amount=repayment_amount,
            transaction_type=transaction_type_for_repayment,
            date=repayment_date,
            description=final_description,
            account_id=account.id,
            related_debt_id=debt.id
            # Категория для погашения долгов обычно не указывается или это спец. категория
        )
        
        debt.repaid_amount += repayment_amount
        if abs(debt.repaid_amount - debt.initial_amount) < 0.001: # Сравнение float
            debt.repaid_amount = debt.initial_amount # Коррекция для точности
            debt.status = 'repaid'
        elif debt.repaid_amount > 0:
            debt.status = 'partially_repaid'
        
        db.session.add(new_repayment_transaction)
        db.session.commit()
        flash('Погашение долга успешно обработано!', 'success')
        return redirect(url_for('ui_debts'))

    accounts_data = Account.query.order_by(Account.name).all()
    return render_template('repay_debt.html', debt=debt, accounts=accounts_data, remaining_debt=remaining_debt, now=datetime.now(timezone.utc))

# --- UI для Правил Кэшбэка ---
@app.route('/ui/cashback-rules')
def ui_cashback_rules():
    rules = CashbackRule.query.order_by(CashbackRule.name).all()
    return render_template('cashback_rules.html', rules=rules)

@app.route('/ui/add-cashback-rule', methods=['GET', 'POST'])
def ui_add_cashback_rule_form():
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')
        cashback_percentage_str = request.form.get('cashback_percentage')
        applies_to_category_id = request.form.get('applies_to_category_id', type=int)
        credit_to_account_id = request.form.get('credit_to_account_id', type=int)
        is_active = 'is_active' in request.form

        if not all([name, cashback_percentage_str, applies_to_category_id, credit_to_account_id]):
            flash('Заполните все обязательные поля: Название, Процент, Категория, Счет зачисления.', 'danger')
            return redirect(url_for('ui_add_cashback_rule_form'))

        try:
            cashback_percentage = float(cashback_percentage_str) / 100.0 # Пользователь вводит %, храним как долю
            if not (0 < cashback_percentage <= 1): # Процент должен быть от >0 до 100%
                raise ValueError("Percentage out of range")
        except ValueError:
            flash('Некорректный процент кэшбэка. Введите число от 0.01 до 100.', 'danger')
            return redirect(url_for('ui_add_cashback_rule_form'))
        
        category = Category.query.filter_by(id=applies_to_category_id, type='expense').first()
        if not category:
            flash('Выбранная категория расходов не найдена или не является расходной.', 'danger')
            return redirect(url_for('ui_add_cashback_rule_form'))

        account = Account.query.get(credit_to_account_id)
        if not account:
            flash('Счет для зачисления кэшбэка не найден.', 'danger')
            return redirect(url_for('ui_add_cashback_rule_form'))
        
        existing_rule = CashbackRule.query.filter_by(name=name).first()
        if existing_rule:
            flash(f"Правило с названием '{name}' уже существует.", 'warning')
            return redirect(url_for('ui_add_cashback_rule_form'))

        new_rule = CashbackRule(
            name=name,
            description=description,
            cashback_percentage=cashback_percentage,
            applies_to_category_id=category.id,
            credit_to_account_id=account.id,
            is_active=is_active
        )
        db.session.add(new_rule)
        db.session.commit()
        flash(f"Правило кэшбэка '{name}' успешно создано!", 'success')
        return redirect(url_for('ui_cashback_rules'))

    # Для формы нужны только расходные категории
    expense_categories = Category.query.filter_by(type='expense').order_by(Category.name).all()
    accounts = Account.query.order_by(Account.name).all()
    return render_template('add_cashback_rule.html', expense_categories=expense_categories, accounts=accounts)

@app.route('/ui/cashback-rules/<int:rule_id>/edit', methods=['GET', 'POST'])
def ui_edit_cashback_rule_form(rule_id):
    rule = CashbackRule.query.get_or_404(rule_id)
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')
        cashback_percentage_str = request.form.get('cashback_percentage')
        applies_to_category_id = request.form.get('applies_to_category_id', type=int)
        credit_to_account_id = request.form.get('credit_to_account_id', type=int)
        is_active = 'is_active' in request.form

        if not all([name, cashback_percentage_str, applies_to_category_id, credit_to_account_id]):
            flash('Заполните все обязательные поля: Название, Процент, Категория, Счет зачисления.', 'danger')
            return redirect(url_for('ui_edit_cashback_rule_form', rule_id=rule_id))

        try:
            cashback_percentage = float(cashback_percentage_str) / 100.0
            if not (0 < cashback_percentage <= 1):
                raise ValueError("Percentage out of range")
        except ValueError:
            flash('Некорректный процент кэшбэка. Введите число от 0.01 до 100.', 'danger')
            return redirect(url_for('ui_edit_cashback_rule_form', rule_id=rule_id))

        category = Category.query.filter_by(id=applies_to_category_id, type='expense').first()
        if not category:
            flash('Выбранная категория расходов не найдена или не является расходной.', 'danger')
            return redirect(url_for('ui_edit_cashback_rule_form', rule_id=rule_id))

        account = Account.query.get(credit_to_account_id)
        if not account:
            flash('Счет для зачисления кэшбэка не найден.', 'danger')
            return redirect(url_for('ui_edit_cashback_rule_form', rule_id=rule_id))

        # Проверка, если имя изменилось и новое имя уже занято другим правилом
        if name != rule.name:
            existing_rule_with_new_name = CashbackRule.query.filter(CashbackRule.name == name, CashbackRule.id != rule_id).first()
            if existing_rule_with_new_name:
                flash(f"Правило с названием '{name}' уже существует.", 'warning')
                return redirect(url_for('ui_edit_cashback_rule_form', rule_id=rule_id))

        rule.name = name
        rule.description = description
        rule.cashback_percentage = cashback_percentage
        rule.applies_to_category_id = category.id
        rule.credit_to_account_id = account.id
        rule.is_active = is_active
        
        db.session.commit()
        flash(f"Правило кэшбэка '{rule.name}' успешно обновлено!", 'success')
        return redirect(url_for('ui_cashback_rules'))

    expense_categories = Category.query.filter_by(type='expense').order_by(Category.name).all()
    accounts = Account.query.order_by(Account.name).all()
    return render_template('edit_cashback_rule.html', rule=rule, expense_categories=expense_categories, accounts=accounts)

@app.route('/ui/cashback-rules/<int:rule_id>/delete', methods=['POST'])
def ui_delete_cashback_rule(rule_id):
    rule_to_delete = CashbackRule.query.get_or_404(rule_id)
    # Дополнительные проверки (например, если есть связанные активные процессы) можно добавить здесь
    db.session.delete(rule_to_delete)
    db.session.commit()
    flash(f"Правило кэшбэка '{rule_to_delete.name}' успешно удалено.", 'success')
    return redirect(url_for('ui_cashback_rules'))

@app.route('/ui/cashback-rules/<int:rule_id>/toggle-active', methods=['POST'])
def ui_toggle_cashback_rule_status(rule_id):
    rule = CashbackRule.query.get_or_404(rule_id)
    rule.is_active = not rule.is_active
    db.session.commit()
    status_text = "активировано" if rule.is_active else "деактивировано"
    flash(f"Правило '{rule.name}' было {status_text}.", 'info')
    return redirect(url_for('ui_cashback_rules'))

@app.route('/ui/analytics')
def ui_analytics_overview():
    total_balance = get_total_balance_data()
    all_accounts = Account.query.order_by(Account.name).all()
    
    today = datetime.now(timezone.utc).date() # Use date object for comparisons
    
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    if start_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    else:
        start_date = today.replace(day=1) # Default to first day of current month

    if end_date_str:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    else:
        end_date = today # Default to today

    account_id_filter = request.args.get('account_id', type=int)
        
    financial_summary_data = get_financial_summary_data(
        start_date=start_date.strftime('%Y-%m-%d'),
        end_date=end_date.strftime('%Y-%m-%d'),
        account_id=account_id_filter
    )
    debt_summary_data = get_debt_summary_data() # Debt summary currently doesn't use date filters

    return render_template('analytics_overview.html',
                           total_balance_by_currency=total_balance,
                           all_accounts=all_accounts,
                           financial_summary=financial_summary_data,
                           debt_summary=debt_summary_data,
                           current_start_date=start_date.strftime('%Y-%m-%d'),
                           current_end_date=end_date.strftime('%Y-%m-%d'),
                           selected_account_id=account_id_filter)

def get_financial_summary_data(start_date=None, end_date=None, account_id=None):
    # ... (существующий код для доходов и расходов) ...
    query = db.session.query(
        Category.name,
        Category.type,
        db.func.sum(Transaction.amount).label('total_amount'),
        Account.currency
    ).join(Transaction, Category.id == Transaction.category_id)\
     .join(Account, Transaction.account_id == Account.id)

    if start_date:
        query = query.filter(Transaction.date >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        query = query.filter(Transaction.date <= datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59))
    
    target_account_object = None
    if account_id:
        target_account_object = Account.query.get(account_id)
        if target_account_object: # Check if account exists
            query = query.filter(Transaction.account_id == account_id)
    
    excluded_transaction_types = ['transfer', 'debt_repayment_expense', 'debt_repayment_income']
    # Исключаем транзакции, которые являются результатом начисления кэшбэка,
    # чтобы они не дублировались, если мы считаем кэшбэк отдельно.
    # Это можно сделать, если у кэшбэк-транзакций есть специальная категория "Кэшбэк".
    # Либо, если мы хотим, чтобы "Кэшбэк" был частью "Доходов", то эту строку не добавляем.
    # Пока оставим как есть, "Кэшбэк" будет в доходах.
    query = query.filter(Transaction.transaction_type.notin_(excluded_transaction_types))
    summary_raw = query.group_by(Category.name, Category.type, Account.currency).order_by(Category.type, Category.name).all()
    
    result = {
        'income_by_category': [], 'expense_by_category': [],
        'total_income': {}, 'total_expense': {}, 'net_flow': {},
        'total_cashback_period': {} # Новое поле для кэшбэка за период
    }
    for category_name, category_type, total, currency in summary_raw:
        item = {'category': category_name, 'total_amount': round(total or 0, 2), 'currency': currency}
        if category_type == 'income':
            result['income_by_category'].append(item)
            result['total_income'][currency] = round(result['total_income'].get(currency, 0) + (total or 0), 2)
        elif category_type == 'expense':
            result['expense_by_category'].append(item)
            result['total_expense'][currency] = round(result['total_expense'].get(currency, 0) + (total or 0), 2)
    
    all_currencies_flow = set(result['total_income'].keys()) | set(result['total_expense'].keys())
    for curr in all_currencies_flow:
        income = result['total_income'].get(curr, 0)
        expense = result['total_expense'].get(curr, 0)
        result['net_flow'][curr] = round(income - expense, 2)

    # Рассчитываем кэшбэк за период
    cashback_query = db.session.query(
        Cashback.currency,
        db.func.sum(Cashback.amount).label('total_cashback_amount')
    ).join(Account, Cashback.account_id == Account.id) # Присоединяем Account для фильтрации по валюте счета, если нужно

    if start_date:
        cashback_query = cashback_query.filter(Cashback.date_received >= datetime.strptime(start_date, '%Y-%m-%d'))
    if end_date:
        cashback_query = cashback_query.filter(Cashback.date_received <= datetime.strptime(end_date, '%Y-%m-%d').replace(hour=23, minute=59, second=59))
    
    if target_account_object: # Если фильтруем по конкретному счету
        # Фильтруем кэшбэки, зачисленные на этот счет ИЛИ если исходная транзакция была с этого счета
        # Для простоты, пока будем считать кэшбэки, зачисленные на ЛЮБОЙ счет пользователя, если не указан конкретный счет.
        # Если account_id указан, то кэшбэки, зачисленные на этот счет.
        cashback_query = cashback_query.filter(Cashback.account_id == account_id)


    cashback_period_raw = cashback_query.group_by(Cashback.currency).all()
    for currency, total_cb in cashback_period_raw:
        result['total_cashback_period'][currency] = round(total_cb or 0, 2)
        
    return result

def get_debt_summary_data():
    active_debts = Debt.query.filter(Debt.status.in_(['active', 'partially_repaid'])).all()
    summary = {
        'total_i_owe': {}, 'total_owed_to_me': {},
        'details': {'i_owe': [], 'owed_to_me': []}
    }
    if not active_debts: return summary
    for debt in active_debts:
        remaining_amount = round(debt.initial_amount - debt.repaid_amount, 2)
        currency = debt.currency
        debt_detail = {
            'id': debt.id, 'counterparty': debt.counterparty, 'description': debt.description,
            'initial_amount': debt.initial_amount, 'repaid_amount': debt.repaid_amount,
            'remaining_amount': remaining_amount, 'currency': currency,
            'due_date': debt.due_date.strftime('%Y-%m-%d') if debt.due_date else None
        }
        if debt.debt_type == 'i_owe':
            summary['total_i_owe'][currency] = summary['total_i_owe'].get(currency, 0) + remaining_amount
            summary['details']['i_owe'].append(debt_detail)
        elif debt.debt_type == 'owed_to_me':
            summary['total_owed_to_me'][currency] = summary['total_owed_to_me'].get(currency, 0) + remaining_amount
            summary['details']['owed_to_me'].append(debt_detail)
    for curr_key in summary['total_i_owe']: summary['total_i_owe'][curr_key] = round(summary['total_i_owe'][curr_key], 2)
    for curr_key in summary['total_owed_to_me']: summary['total_owed_to_me'][curr_key] = round(summary['total_owed_to_me'][curr_key], 2)
    return summary

# --- API Маршруты (Endpoints) ---
# Модификация API для добавления транзакции для учета кэшбэка
@app.route('/api/transactions', methods=['POST'])
def api_add_transaction():
    data = request.get_json()
    required_fields = ['amount', 'transaction_type', 'account_id']
    if not data or not all(field in data for field in required_fields):
        return jsonify({'error': 'Missing required fields: amount, transaction_type, account_id'}), 400

    account = Account.query.get(data['account_id'])
    if not account:
        return jsonify({'error': 'Account not found'}), 404

    category = None
    if data.get('category_id'):
        category = Category.query.get(data['category_id'])
        if not category and data['transaction_type'] != 'transfer':
            return jsonify({'error': 'Category not found'}), 404
        if category:
            if data['transaction_type'] == 'income' and category.type != 'income':
                return jsonify({'error': f'Category "{category.name}" is not an income category'}), 400
            if data['transaction_type'] == 'expense' and category.type != 'expense':
                return jsonify({'error': f'Category "{category.name}" is not an expense category'}), 400
    
    if data['transaction_type'] in ['income', 'expense'] and not data.get('category_id'):
        return jsonify({'error': 'Category is required for income/expense transactions'}), 400


    transaction_type = data['transaction_type']
    amount = float(data['amount'])

    # Основные типы для этого эндпоинта. Погашения долгов обрабатываются через свои эндпоинты.
    if transaction_type not in ['income', 'expense', 'transfer']: 
        return jsonify({'error': 'Invalid transaction_type for this endpoint. Use "income", "expense", or "transfer".'}), 400
    
    if amount <= 0:
        return jsonify({'error': 'Amount must be positive'}), 400

    new_transaction = Transaction(
        amount=amount,
        transaction_type=transaction_type,
        date=datetime.strptime(data['date'], '%Y-%m-%d') if data.get('date') else datetime.now(timezone.utc),
        description=data.get('description'),
        account_id=account.id,
        category_id=category.id if category and transaction_type != 'transfer' else None
    )

    if transaction_type == 'income':
        account.balance += amount
    elif transaction_type == 'expense':
        if account.balance < amount and account.type != 'credit_card':
            # Не возвращаем ошибку, а позволяем, но можно добавить предупреждение в ответ
            pass # return jsonify({'error': 'Insufficient funds'}), 400 
        account.balance -= amount
    elif transaction_type == 'transfer':
        to_account_id = data.get('to_account_id')
        if not to_account_id:
            return jsonify({'error': 'Missing to_account_id for transfer'}), 400
        if to_account_id == account.id:
            return jsonify({'error': 'Cannot transfer to the same account'}), 400
            
        to_account = Account.query.get(to_account_id)
        if not to_account:
            return jsonify({'error': 'Destination account not found'}), 404
        
        if account.currency != to_account.currency:
             return jsonify({'error': f'Currency mismatch for transfer: {account.currency} to {to_account.currency}. Automatic conversion not supported via API.'}), 400
        
        if account.balance < amount and account.type != 'credit_card':
             # Не возвращаем ошибку, а позволяем, но можно добавить предупреждение в ответ
            pass # return jsonify({'error': 'Insufficient funds for transfer'}), 400
        
        account.balance -= amount
        to_account.balance += amount
        new_transaction.to_account_id = to_account_id
        new_transaction.category_id = None 

    db.session.add(new_transaction)
    
    # Применение правил кэшбэка для API
    if new_transaction.transaction_type == 'expense':
        db.session.flush() # Чтобы new_transaction получил ID
        _apply_cashback_rules(new_transaction) # Используем ту же внутреннюю функцию

    db.session.commit()

    return jsonify({
        'id': new_transaction.id, 
        'message': 'Transaction added successfully via API',
        'account_balance': account.balance # Возвращаем баланс основного счета транзакции
    }), 201


# --- Инициализация БД и запуск приложения ---
if __name__ == '__main__':
    # db.create_all() has been moved to run on app initialization for services like Render
    # For local development, if you need to re-create tables from scratch and are not using migrations for it:
    # with app.app_context():
    #     db.drop_all() # Be careful, this deletes all data!
    #     db.create_all()
    app.run(debug=True)