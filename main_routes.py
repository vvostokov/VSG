import os
import json
from datetime import datetime, date, timezone, timedelta
from collections import namedtuple, defaultdict
from flask import (Blueprint, render_template, request, redirect, url_for, flash, current_app, g, jsonify) # noqa
from decimal import Decimal, InvalidOperation
from sqlalchemy.orm import joinedload
from sqlalchemy import func, asc, desc, or_
from models import Debt, Account, BankingTransaction, Bank
from extensions import db # noqa
from models import (
    InvestmentPlatform, InvestmentAsset, Transaction, Account, Category, Debt,
    BankingTransaction, HistoricalPriceCache, CryptoPortfolioHistory, JsonCache,
    SecuritiesPortfolioHistory, TransactionItem,
)
from api_clients import (
    SYNC_DISPATCHER, SYNC_TRANSACTIONS_DISPATCHER, PRICE_TICKER_DISPATCHER,
    _convert_bybit_timestamp, fetch_bybit_spot_tickers, fetch_bitget_spot_tickers,
    fetch_bingx_spot_tickers, fetch_kucoin_spot_tickers, fetch_okx_spot_tickers,
    fetch_cryptocompare_news,
    TRANSACTION_PROCESSOR_DISPATCHER
)
from analytics_logic import (
    refresh_crypto_price_change_data, refresh_crypto_portfolio_history, refresh_securities_portfolio_history,
    get_performance_chart_data_from_cache, refresh_performance_chart_data, refresh_market_leaders_cache)
from securities_logic import fetch_moex_market_leaders, fetch_moex_securities_metadata # noqa
from news_logic import get_crypto_news, get_securities_news
from logic.news_analysis import get_news_trends_for_portfolio
from logic.platform_sync_logic import sync_platform_balances, sync_platform_transactions

main_bp = Blueprint('main', __name__)

def _get_currency_rates():
    """
    Возвращает словарь с курсами валют к рублю.
    Пытается получить актуальный курс USDT/RUB из кэша. Если кэш пуст,
    запрашивает курс напрямую. В случае ошибки использует значения по умолчанию.
    """
    if 'currency_rates' not in g:
        # Значения по умолчанию на случай, если API или кэш недоступны
        rates = {
            'USD': Decimal('90.0'), 
            'EUR': Decimal('100.0'), 
            'RUB': Decimal('1.0'), 
            'USDT': Decimal('90.0'), 
            None: Decimal('1.0') # Для активов без указания валюты
        }
        try:
            cache_entry = JsonCache.query.filter_by(cache_key='currency_rates').first()
            if cache_entry and cache_entry.json_data:
                cached_rates = json.loads(cache_entry.json_data)
                # Обновляем курсы из кэша, конвертируя строки в Decimal
                if 'USDT' in cached_rates:
                    rates['USDT'] = Decimal(cached_rates['USDT'])
                if 'USD' in cached_rates:
                    rates['USD'] = Decimal(cached_rates['USD'])
                current_app.logger.info(f"--- [Currency Rates] Курсы валют загружены из кэша: USDT={rates['USDT']}")
            else:
                # Если кэш пуст, пытаемся получить курс напрямую и создать кэш
                current_app.logger.info("--- [Currency Rates] Кэш курсов пуст, попытка получить свежий курс...")
                from api_clients import fetch_usdt_rub_rate # Локальный импорт для избежания циклической зависимости
                fresh_rate = fetch_usdt_rub_rate()
                if fresh_rate:
                    rates['USDT'] = fresh_rate
                    rates['USD'] = fresh_rate
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"--- [Currency Rates] Ошибка при получении курсов из кэша, используются значения по умолчанию. Ошибка: {e}")
        
        g.currency_rates = rates
    return g.currency_rates

def _populate_account_from_form(account: Account, form_data):
    """Вспомогательная функция для заполнения объекта Account из данных формы."""
    account.name = form_data.get('name')
    account.account_type = form_data.get('account_type')
    account.currency = form_data.get('currency')
    account.balance = Decimal(form_data.get('balance', '0'))
    account.is_active = 'is_active' in form_data
    interest_rate_str = form_data.get('interest_rate')
    account.bank_id = int(form_data.get('bank_id')) if form_data.get('bank_id') else None
    account.interest_rate = Decimal(interest_rate_str) if interest_rate_str and interest_rate_str.strip() else None
    start_date_str = form_data.get('start_date')
    account.start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str and start_date_str.strip() else None
    end_date_str = form_data.get('end_date')
    account.end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str and end_date_str.strip() else None
    account.notes = form_data.get('notes')

    if account.account_type == 'credit':
        account.credit_limit = Decimal(form_data.get('credit_limit', '0'))
        account.grace_period_days = int(form_data.get('grace_period_days', '0'))
def _calculate_portfolio_changes(history_records: list) -> dict:
    """Рассчитывает процентные изменения портфеля для разных периодов."""
    changes = {'1d': None, '7d': None, '30d': None, '180d': None, '365d': None}
    if not history_records:
        return changes

    history_by_date = {record.date: record.total_value_rub for record in history_records}
    
    # Находим самую последнюю доступную дату в истории как "текущую"
    latest_date = max(history_by_date.keys())
    latest_val = history_by_date[latest_date]

    periods = {'1d': 1, '7d': 7, '30d': 30, '180d': 180, '365d': 365}
    for period_name, days_ago in periods.items():
        past_date = latest_date - timedelta(days=days_ago)
        past_val = history_by_date.get(past_date)
        
        if past_val is not None and past_val > 0:
            change_pct = ((latest_val - past_val) / past_val) * 100
            changes[period_name] = change_pct
            
    return changes

@main_bp.route('/')
def index():
    # --- Константы и курсы ---
    currency_rates_to_rub = _get_currency_rates()

    # --- 1. Сводка по портфелю ценных бумаг ---
    securities_assets = InvestmentAsset.query.join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'stock_broker').all()
    securities_total_rub = sum(
        (asset.quantity or 0) * (asset.current_price or 0) * currency_rates_to_rub.get(asset.currency_of_price, Decimal(1.0))
        for asset in securities_assets
    )
    # Расчет изменений для портфеля ЦБ
    securities_history_start_date = date.today() - timedelta(days=366)
    securities_history = SecuritiesPortfolioHistory.query.filter(SecuritiesPortfolioHistory.date >= securities_history_start_date).order_by(SecuritiesPortfolioHistory.date.asc()).all()
    securities_changes = _calculate_portfolio_changes(securities_history)

    # --- 2. Сводка по крипто-портфелю --- # noqa
    crypto_assets = InvestmentAsset.query.join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'crypto_exchange').all()
    crypto_total_usdt = sum((asset.quantity or 0) * (asset.current_price or 0) for asset in crypto_assets)
    crypto_total_rub = crypto_total_usdt * currency_rates_to_rub['USDT']

    # Расчет изменений для крипто-портфеля за разные периоды # noqa
    start_date_query = date.today() - timedelta(days=366)
    crypto_history = CryptoPortfolioHistory.query.filter(CryptoPortfolioHistory.date >= start_date_query).order_by(CryptoPortfolioHistory.date.asc()).all()
    crypto_changes = _calculate_portfolio_changes(crypto_history)

    # --- 3. Сводка по банковским счетам (включая кредитные карты) ---
    bank_accounts = Account.query.filter(Account.account_type.in_(['bank_account', 'deposit', 'bank_card', 'credit'])).all()
    banking_total_rub = Decimal(0)
    for acc in bank_accounts:
        value_in_rub = acc.balance * currency_rates_to_rub.get(acc.currency, Decimal(1.0))
        if acc.account_type == 'credit':
            banking_total_rub -= value_in_rub # Вычитаем долг по кредитке
        else:
            banking_total_rub += value_in_rub # Прибавляем активы
    
    # Список вкладов и накопительных счетов для отображения
    deposits_and_savings = Account.query.filter(
        Account.account_type.in_(['deposit', 'bank_account']),
        Account.is_active == True
    ).order_by(Account.balance.desc()).all()

    # --- 4. Сводка по долгам ---
    i_owe_list = Debt.query.filter_by(debt_type='i_owe', status='active').all()
    owed_to_me_list = Debt.query.filter_by(debt_type='owed_to_me', status='active').all()
    # TODO: Добавить конвертацию валют для долгов
    i_owe_total_rub = sum(d.initial_amount - d.repaid_amount for d in i_owe_list)
    owed_to_me_total_rub = sum(d.initial_amount - d.repaid_amount for d in owed_to_me_list)

    # --- 5. Последние операции ---
    last_investment_txs = Transaction.query.options(joinedload(Transaction.platform)).order_by(Transaction.timestamp.desc()).limit(7).all()
    last_banking_txs = BankingTransaction.query.options(
        joinedload(BankingTransaction.account_ref), 
        joinedload(BankingTransaction.to_account_ref)
    ).order_by(BankingTransaction.date.desc()).limit(7).all()

    combined_txs = []
    for tx in last_investment_txs:
        desc = tx.raw_type or tx.type.capitalize()
        value_str = ""
        if tx.type == 'buy':
            desc = f"Покупка {tx.asset1_ticker}"
            value_str = f"-{tx.asset2_amount:,.2f}".replace(',', ' ') + f" {tx.asset2_ticker}"
        elif tx.type == 'sell':
            desc = f"Продажа {tx.asset1_ticker}"
            value_str = f"+{tx.asset2_amount:,.2f}".replace(',', ' ') + f" {tx.asset2_ticker}"
        elif tx.type == 'deposit':
            desc = f"Депозит {tx.asset1_ticker}"
            value_str = f"+{tx.asset1_amount:,.4f}".replace(',', ' ').rstrip('0').rstrip('.') + f" {tx.asset1_ticker}"
        elif tx.type == 'withdrawal':
            desc = f"Вывод {tx.asset1_ticker}"
            value_str = f"-{tx.asset1_amount:,.4f}".replace(',', ' ').rstrip('0').rstrip('.') + f" {tx.asset1_ticker}"
        elif tx.type == 'transfer':
            desc = f"Перевод {tx.asset1_ticker}"
            value_str = f"{tx.asset1_amount:,.4f}".replace(',', ' ').rstrip('0').rstrip('.') + f" {tx.asset1_ticker}"
        
        combined_txs.append({
            'timestamp': tx.timestamp,
            'description': desc,
            'value': value_str,
            'source': tx.platform.name,
            'is_investment': True,
            'is_positive': None
        })

    for tx in last_banking_txs:
        desc = tx.description or tx.transaction_type.capitalize()
        value_str = ""
        is_positive = None
        if tx.transaction_type == 'expense':
            value_str = f"-{tx.amount:,.2f}".replace(',', ' ') + f" {tx.account_ref.currency}"
            is_positive = False
        elif tx.transaction_type == 'income':
            value_str = f"+{tx.amount:,.2f}".replace(',', ' ') + f" {tx.account_ref.currency}"
            is_positive = True
        elif tx.transaction_type == 'transfer':
            desc = f"Перевод на {tx.to_account_ref.name}"
            value_str = f"-{tx.amount:,.2f}".replace(',', ' ') + f" {tx.account_ref.currency}"
            is_positive = False
        elif tx.transaction_type == 'exchange':
            desc = f"Обмен {tx.account_ref.currency} -> {tx.to_account_ref.currency}"
            value_str = f"+{tx.to_amount:,.2f}".replace(',', ' ') + f" {tx.to_account_ref.currency}"
            is_positive = True
        
        combined_txs.append({
            'timestamp': tx.date,
            'description': desc,
            'value': value_str,
            'source': tx.account_ref.name,
            'is_investment': False,
            'is_positive': is_positive
        })

    combined_txs.sort(key=lambda x: x['timestamp'], reverse=True)
    last_10_transactions = combined_txs[:10]

    # --- 5.1 Последние операции по ЦБ ---
    last_securities_txs_raw = Transaction.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'stock_broker'
    ).options(joinedload(Transaction.platform)).order_by(Transaction.timestamp.desc()).limit(10).all()

    last_securities_txs = []
    for tx in last_securities_txs_raw:
        desc = tx.raw_type or tx.type.capitalize()
        value_str = ""
        is_positive = None
        if tx.type == 'buy':
            desc = f"Покупка {tx.asset1_ticker}"
            value_str = f"-{tx.asset2_amount:,.2f}".replace(',', ' ') + f" {tx.asset2_ticker}"
            is_positive = False
        elif tx.type == 'sell':
            desc = f"Продажа {tx.asset1_ticker}"
            value_str = f"+{tx.asset2_amount:,.2f}".replace(',', ' ') + f" {tx.asset2_ticker}"
            is_positive = True
        
        last_securities_txs.append({
            'timestamp': tx.timestamp, 'description': desc, 'value': value_str,
            'source': tx.platform.name, 'is_positive': is_positive
        })

    # --- 6. Общая стоимость ---
    net_worth_rub = securities_total_rub + crypto_total_rub + banking_total_rub + owed_to_me_total_rub - i_owe_total_rub

    # --- 7. Топ-5 активов по стоимости ---
    # --- Top 5 Securities ---
    securities_valued_assets = []
    for asset in securities_assets:
        value_rub = (asset.quantity or 0) * (asset.current_price or 0) * currency_rates_to_rub.get(asset.currency_of_price, Decimal(1.0))
        securities_valued_assets.append({'asset': asset, 'value_rub': value_rub})

    top_5_securities_sorted = sorted(securities_valued_assets, key=lambda x: x['value_rub'], reverse=True)[:5]
    top_securities_isins = [item['asset'].ticker for item in top_5_securities_sorted]

    securities_price_changes_raw = db.session.query(
        HistoricalPriceCache.ticker, 
        HistoricalPriceCache.period, 
        HistoricalPriceCache.change_percent
    ).filter(
        HistoricalPriceCache.ticker.in_(top_securities_isins),
        HistoricalPriceCache.period.in_(['1d', '7d', '30d'])
    ).all()

    securities_changes_by_isin = defaultdict(dict)
    for isin, period, change in securities_price_changes_raw:
        securities_changes_by_isin[isin][period] = change

    top_5_securities = []
    for item in top_5_securities_sorted:
        isin = item['asset'].ticker
        item['changes'] = securities_changes_by_isin.get(isin, {})
        top_5_securities.append(item)

    # --- Top 5 Crypto ---
    # ИЗМЕНЕНО: Логика определения топ-5 криптоактивов.
    # Теперь активы сначала агрегируются по тикеру со всех платформ, а затем сортируются.
    aggregated_crypto_assets = defaultdict(lambda: {
        'total_quantity': Decimal(0),
        'total_value_rub': Decimal(0),
        'name': ''
    })

    for asset in crypto_assets: # crypto_assets уже получен ранее
        ticker = asset.ticker
        quantity = asset.quantity or Decimal(0)
        price = asset.current_price or Decimal(0)
        
        asset_value_usdt = quantity * price
        asset_value_rub = asset_value_usdt * currency_rates_to_rub.get('USDT', Decimal(1.0))

        agg = aggregated_crypto_assets[ticker]
        agg['total_quantity'] += quantity
        agg['total_value_rub'] += asset_value_rub
        agg['name'] = asset.name # Имя будет одинаковым для одного тикера

    top_5_crypto_sorted = sorted(aggregated_crypto_assets.items(), key=lambda item: item[1]['total_value_rub'], reverse=True)[:5]
    top_crypto_tickers = [ticker for ticker, data in top_5_crypto_sorted]

    crypto_price_changes_raw = db.session.query(
        HistoricalPriceCache.ticker, 
        HistoricalPriceCache.period, 
        HistoricalPriceCache.change_percent
    ).filter(
        HistoricalPriceCache.ticker.in_(top_crypto_tickers),
        HistoricalPriceCache.period.in_(['24h', '7d', '30d'])
    ).all()

    crypto_changes_by_ticker = defaultdict(dict)
    for ticker, period, change in crypto_price_changes_raw:
        period_key = '1d' if period == '24h' else period
        crypto_changes_by_ticker[ticker][period_key] = change

    top_5_crypto = []
    for ticker, data in top_5_crypto_sorted:
        top_5_crypto.append({
            'ticker': ticker,
            'name': data['name'],
            'value_rub': data['total_value_rub'],
            'changes': crypto_changes_by_ticker.get(ticker, {})
        })

    return render_template(
        'index.html',
        net_worth_rub=net_worth_rub,
        securities_summary={'total_rub': securities_total_rub, 'changes': securities_changes}, # noqa
        crypto_summary={'total_rub': crypto_total_rub, 'changes': crypto_changes},
        banking_summary={'total_rub': banking_total_rub},
        debt_summary={'i_owe': i_owe_total_rub, 'owed_to_me': owed_to_me_total_rub},
        last_transactions=last_10_transactions,
        last_securities_txs=last_securities_txs,
        deposits_and_savings=deposits_and_savings,
        top_5_securities=top_5_securities,
        top_5_crypto=top_5_crypto
    )

@main_bp.route('/platforms')
def ui_investment_platforms():
    # Фильтруем платформы, чтобы на странице отображались только криптобиржи.
    # Для брокеров ценных бумаг существует отдельная страница.
    platforms = InvestmentPlatform.query.filter_by(platform_type='crypto_exchange').order_by(InvestmentPlatform.name).all()
    return render_template('investment_platforms.html', platforms=platforms)

@main_bp.route('/platforms/add', methods=['GET', 'POST'])
def ui_add_investment_platform_form():
    if request.method == 'POST':
        manual_earn_balances_input = request.form.get('manual_earn_balances_input', '{}')
        try:
            json.loads(manual_earn_balances_input)
        except json.JSONDecodeError:
            flash('Неверный формат JSON для ручных Earn балансов. Используйте {"TICKER": "QUANTITY"}.', 'danger')
            current_data = request.form.to_dict()
            return render_template('add_investment_platform.html', current_data=current_data)

        new_platform = InvestmentPlatform(
            name=request.form['name'],
            platform_type=request.form['platform_type'],
            api_key=request.form.get('api_key'), # api_key можно хранить открытым
            api_secret=request.form.get('api_secret'), # Используем сеттер, который зашифрует
            passphrase=request.form.get('passphrase'), # Используем сеттер, который зашифрует
            other_credentials_json=request.form.get('other_credentials_json'), # Используем сеттер
            notes=request.form.get('notes'),
            is_active='is_active' in request.form,
            manual_earn_balances_json=manual_earn_balances_input
        )
        db.session.add(new_platform)
        db.session.commit()
        flash(f'Платформа "{new_platform.name}" успешно добавлена.', 'success')
        return redirect(url_for('main.ui_investment_platforms'))
    return render_template('add_investment_platform.html', current_data={})

@main_bp.route('/platforms/<int:platform_id>')
def ui_investment_platform_detail(platform_id):
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    currency_rates_to_rub = _get_currency_rates()

    all_valued_assets = []
    platform_total_value_rub = Decimal(0)
    platform_total_value_usdt = Decimal(0)
    account_type_summary = defaultdict(lambda: {'rub': Decimal(0), 'usdt': Decimal(0)})
    assets_with_balance = platform.assets.filter(InvestmentAsset.quantity > 0).order_by(InvestmentAsset.source_account_type, InvestmentAsset.ticker)
    for asset in assets_with_balance:
        quantity = asset.quantity or Decimal(0)
        price = asset.current_price or Decimal(0)
        rate = currency_rates_to_rub.get(asset.currency_of_price, Decimal(1.0))

        asset_value_usdt = quantity * price
        platform_total_value_usdt += asset_value_usdt

        asset_value_rub = asset_value_usdt * rate
        platform_total_value_rub += asset_value_rub
        
        account_type = asset.source_account_type or 'Unknown'
        account_type_summary[account_type]['rub'] += asset_value_rub
        account_type_summary[account_type]['usdt'] += asset_value_usdt
            
        all_valued_assets.append({'asset': asset, 'value_rub': asset_value_rub, 'value_usdt': asset_value_usdt})

    manual_earn_balances = {}
    try:
        manual_earn_balances = json.loads(platform.manual_earn_balances_json)
    except (json.JSONDecodeError, TypeError):
        flash('Ошибка чтения ручных Earn балансов (неверный JSON). Пожалуйста, исправьте.', 'danger')
        manual_earn_balances = {}

    # ОПТИМИЗАЦИЯ: Сначала собираем все тикеры, затем делаем один запрос на получение цен.
    manual_tickers_to_fetch = [t for t, q_str in manual_earn_balances.items() if Decimal(q_str) > 0 and t.upper() not in ['USDT', 'USDC', 'DAI']]
    manual_prices = {}
    price_fetcher_config = PRICE_TICKER_DISPATCHER.get(platform.name.lower())
    if price_fetcher_config and manual_tickers_to_fetch:
        try:
            symbols_for_api = [f"{ticker}{price_fetcher_config['suffix']}" for ticker in manual_tickers_to_fetch]
            ticker_data_list = price_fetcher_config['func'](target_symbols=symbols_for_api)
            for item in ticker_data_list:
                manual_prices[item['ticker']] = Decimal(item['price'])
        except Exception as e:
            current_app.logger.error(f"Ошибка получения цен для ручных Earn балансов: {e}")

    for ticker, quantity_str in manual_earn_balances.items():
        try:
            quantity = Decimal(quantity_str)
            if quantity <= 0: continue

            currency_of_price = 'USDT'
            current_price = manual_prices.get(ticker)

            if current_price is None: # Если цена не была получена
                current_price = Decimal('1.0') if ticker.upper() in ['USDT', 'USDC', 'DAI'] else Decimal('0.0')

            asset_value_usdt = quantity * (current_price or Decimal(0))
            platform_total_value_usdt += asset_value_usdt
            asset_value_rub = asset_value_usdt * currency_rates_to_rub.get(currency_of_price, Decimal(1.0))
            platform_total_value_rub += asset_value_rub
            
            account_type_summary['Manual Earn']['rub'] += asset_value_rub
            account_type_summary['Manual Earn']['usdt'] += asset_value_usdt

            # Создаем временный объект для отображения в шаблоне
            DummyAsset = namedtuple('InvestmentAsset', ['ticker', 'name', 'quantity', 'current_price', 'currency_of_price', 'source_account_type', 'id'])
            all_valued_assets.append({
                'asset': DummyAsset(
                    ticker=ticker, name=f"{ticker} (Ручной Earn)", quantity=quantity,
                    current_price=current_price, currency_of_price=currency_of_price,
                    source_account_type='Manual Earn', id=None
                ),
                'value_rub': asset_value_rub, 'value_usdt': asset_value_usdt
            })
        except InvalidOperation:
            flash(f'Неверное количество для {ticker} в ручных Earn балансах. Проверьте формат.', 'danger')
        except Exception as e:
            print(f"Непредвиденная ошибка при обработке ручного Earn баланса для {ticker}: {e}")
            flash(f'Непредвиденная ошибка при обработке ручного Earn баланса для {ticker}: {e}', 'danger')

    all_valued_assets.sort(key=lambda x: (x['asset'].source_account_type or '', x['asset'].ticker or ''))
    sorted_account_type_summary = sorted(account_type_summary.items(), key=lambda item: item[0])
    
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort_by', 'timestamp')
    order = request.args.get('order', 'desc')
    filter_type = request.args.get('filter_type', 'all')

    transactions_query = platform.transactions

    if filter_type != 'all':
        transactions_query = transactions_query.filter_by(type=filter_type)

    sort_column = getattr(Transaction, sort_by, Transaction.timestamp)
    transactions_query = transactions_query.order_by(desc(sort_column) if order == 'desc' else asc(sort_column))
    
    transactions_pagination = transactions_query.paginate(page=page, per_page=15, error_out=False)
    platform_transactions = transactions_pagination.items

    unique_transaction_types = [t.type for t in platform.transactions.with_entities(Transaction.type).distinct().all()]
    unique_transaction_types.sort()

    return render_template(
        'investment_platform_detail.html', 
        platform=platform, valued_assets=all_valued_assets, platform_total_value_rub=platform_total_value_rub,
        platform_total_value_usdt=platform_total_value_usdt, account_type_summary=sorted_account_type_summary, 
        platform_transactions=platform_transactions, transactions_pagination=transactions_pagination,
        sort_by=sort_by, order=order, filter_type=filter_type, unique_transaction_types=unique_transaction_types
    )

@main_bp.route('/platforms/<int:platform_id>/edit', methods=['GET', 'POST'])
def ui_edit_investment_platform_form(platform_id):
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    if request.method == 'POST':
        manual_earn_balances_input = request.form.get('manual_earn_balances_input', '{}')
        try:
            json.loads(manual_earn_balances_input)
        except json.JSONDecodeError:
            flash('Неверный формат JSON для ручных Earn балансов. Используйте {"TICKER": "QUANTITY"}.', 'danger')
            platform.manual_earn_balances_json = manual_earn_balances_input
            return render_template('edit_investment_platform.html', platform=platform)

        platform.name = request.form['name']
        platform.platform_type = request.form['platform_type']
        # API Key can be nullable, so we can update it directly.
        platform.api_key = request.form.get('api_key')
        
        # Only update encrypted fields if a new value is provided to avoid accidental erasure.
        # The form should present these as empty fields.
        if request.form.get('api_secret'):
            platform.api_secret = request.form.get('api_secret') # Используем сеттер
        if request.form.get('passphrase'):
            platform.passphrase = request.form.get('passphrase') # Используем сеттер
        if request.form.get('other_credentials_json'):
            platform.other_credentials_json = request.form.get('other_credentials_json') # Используем сеттер
        platform.notes = request.form.get('notes')
        platform.is_active = 'is_active' in request.form
        platform.manual_earn_balances_json = manual_earn_balances_input
        
        db.session.commit()
        flash(f'Данные платформы "{platform.name}" успешно обновлены.', 'success')
        return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform.id))
    return render_template('edit_investment_platform.html', platform=platform)

def _get_sync_function(platform_name: str, dispatcher: dict):
    """
    Вспомогательная функция для поиска функции синхронизации в диспетчере.
    Поддерживает "нечеткий" поиск для распространенных имен бирж, чтобы избежать
    проблем из-за опечаток или разных стилей написания (например, 'Kukoin' vs 'KuCoin').
    """
    # Приводим имя к нижнему регистру и убираем пробелы/дефисы для надежности
    name_lower = platform_name.lower().replace('-', '').replace(' ', '')
    
    # 1. Прямое совпадение по очищенному имени
    sync_function = dispatcher.get(name_lower)
    if sync_function:
        return sync_function
        
    # 2. Нечеткий поиск по ключевым словам
    # Это позволяет найти 'kucoin' в 'my kucoin account' или 'kukoin'
    alias_map = {
        'kucoin': ['kukoin'],
        'bybit': [], 'bingx': [], 'bitget': [], 'okx': []
    }
    for canonical, aliases in alias_map.items():
        if canonical in name_lower or any(alias in name_lower for alias in aliases):
            return dispatcher.get(canonical)
            
    return None

@main_bp.route('/platforms/<int:platform_id>/sync', methods=['POST'])
def ui_sync_investment_platform(platform_id):
    """Запускает синхронизацию балансов для платформы, используя централизованную логику."""
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    success, message = sync_platform_balances(platform)
    if success:
        flash(f'Синхронизация балансов для "{platform.name}" завершена. {message}', 'success')
    else:
        flash(f'Ошибка при синхронизации балансов для "{platform.name}": {message}', 'danger')
    return redirect(request.referrer or url_for('main.ui_investment_platform_detail', platform_id=platform.id))

@main_bp.route('/platforms/<int:platform_id>/sync_transactions', methods=['POST'])
def ui_sync_investment_platform_transactions(platform_id):
    """Запускает синхронизацию транзакций для платформы, используя централизованную логику."""
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    success, message = sync_platform_transactions(platform)
    if success:
        flash(f'Синхронизация транзакций для "{platform.name}" завершена. {message}', 'success')
    else:
        flash(f'Ошибка при синхронизации транзакций для "{platform.name}": {message}', 'danger')
    return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform.id))

@main_bp.route('/platforms/<int:platform_id>/delete', methods=['POST'])
def ui_delete_investment_platform(platform_id):
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    platform_name = platform.name
    db.session.delete(platform)
    db.session.commit()
    flash(f'Платформа "{platform_name}" и все связанные с ней данные были удалены.', 'success')
    return redirect(url_for('main.ui_investment_platforms'))

@main_bp.route('/platforms/<int:platform_id>/assets/add', methods=['GET', 'POST'])
def ui_add_investment_asset_form(platform_id):
    """Обрабатывает добавление крипто-актива вручную для платформы."""
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    if platform.platform_type != 'crypto_exchange':
        flash('Добавление активов вручную поддерживается только для крипто-платформ.', 'warning')
        return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform.id))

    if request.method == 'POST':
        try:
            ticker = request.form.get('ticker', '').upper().strip()
            quantity_str = request.form.get('quantity', '0').replace(',', '.')
            source_account_type = request.form.get('source_account_type', 'Manual').strip()

            if not ticker:
                raise ValueError('Тикер является обязательным полем.')
            if not source_account_type:
                raise ValueError('Тип кошелька является обязательным полем.')

            quantity = Decimal(quantity_str)
            if quantity <= 0:
                raise ValueError('Количество должно быть положительным числом.')

            # Проверяем, существует ли уже такой актив для данной платформы и типа кошелька
            existing_asset = InvestmentAsset.query.filter_by(
                platform_id=platform.id,
                ticker=ticker,
                source_account_type=source_account_type
            ).first()

            if existing_asset:
                existing_asset.quantity += quantity
                db.session.commit()
                flash(f'К существующему активу {ticker} ({source_account_type}) добавлено {quantity}.', 'success')
            else:
                # Создаем новый актив и пытаемся получить его цену
                current_price = Decimal('0')
                currency_of_price = 'USDT'
                if ticker.upper() in ['USDT', 'USDC', 'DAI']:
                    current_price = Decimal('1.0')
                else:
                    price_fetcher_config = _get_sync_function(platform.name, PRICE_TICKER_DISPATCHER)
                    if price_fetcher_config:
                        try:
                            api_symbol = f"{ticker}{price_fetcher_config['suffix']}"
                            ticker_data_list = price_fetcher_config['func'](target_symbols=[api_symbol])
                            if ticker_data_list:
                                current_price = Decimal(ticker_data_list[0]['price'])
                                flash(f'Цена для {ticker} была автоматически получена: {current_price} USDT.', 'info')
                        except Exception as e:
                            current_app.logger.warning(f"Не удалось получить цену для {ticker} при ручном добавлении: {e}")
                            flash(f'Не удалось автоматически получить цену для {ticker}.', 'warning')
                
                new_asset = InvestmentAsset(platform_id=platform.id, ticker=ticker, name=ticker, asset_type='crypto', quantity=quantity, current_price=current_price, currency_of_price=currency_of_price, source_account_type=source_account_type)
                db.session.add(new_asset)
                db.session.commit()
                flash(f'Актив {ticker} ({quantity}) успешно добавлен в кошелек {source_account_type}.', 'success')

            return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform.id))
        except (ValueError, InvalidOperation) as e:
            db.session.rollback()
            flash(f'Ошибка в данных: {e}', 'danger')
            return render_template('add_crypto_asset.html', platform=platform, current_data=request.form)
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Ошибка при добавлении крипто-актива: {e}", exc_info=True)
            flash(f'Произошла непредвиденная ошибка: {e}', 'danger')
            return render_template('add_crypto_asset.html', platform=platform, current_data=request.form)

    # Для GET-запроса отображаем форму
    return render_template('add_crypto_asset.html', platform=platform, current_data={})

@main_bp.route('/crypto-assets/<int:asset_id>/edit', methods=['GET', 'POST'])
def ui_edit_investment_asset(asset_id):
    asset = InvestmentAsset.query.get_or_404(asset_id)
    # Разрешаем редактировать только активы, добавленные вручную или не синхронизируемые
    manual_types = ['Manual', 'Manual Earn', 'Staking', 'Lending']
    if asset.source_account_type not in manual_types:
        flash(f'Редактирование актива {asset.ticker} ({asset.source_account_type}) запрещено, так как он синхронизируется автоматически.', 'warning')
        return redirect(url_for('main.ui_investment_platform_detail', platform_id=asset.platform_id))

    if request.method == 'POST':
        try:
            quantity_str = request.form.get('quantity', '0').replace(',', '.')
            source_account_type = request.form.get('source_account_type', '').strip()

            if not source_account_type:
                raise ValueError('Тип кошелька является обязательным полем.')

            quantity = Decimal(quantity_str)
            if quantity < 0: # Разрешаем 0 для фактического обнуления
                raise ValueError('Количество не может быть отрицательным.')

            asset.quantity = quantity
            asset.source_account_type = source_account_type
            
            db.session.commit()
            flash(f'Актив {asset.ticker} успешно обновлен.', 'success')
            return redirect(url_for('main.ui_investment_platform_detail', platform_id=asset.platform_id))

        except (ValueError, InvalidOperation) as e:
            db.session.rollback()
            flash(f'Ошибка в данных: {e}', 'danger')
            return render_template('edit_crypto_asset.html', asset=asset, current_data=request.form)
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Ошибка при редактировании крипто-актива: {e}", exc_info=True)
            flash(f'Произошла непредвиденная ошибка: {e}', 'danger')
            return render_template('edit_crypto_asset.html', asset=asset, current_data=request.form)

    # Для GET-запроса
    return render_template('edit_crypto_asset.html', asset=asset, current_data=asset)

@main_bp.route('/crypto-assets/<int:asset_id>/delete', methods=['POST'])
def ui_delete_investment_asset(asset_id):
    asset = InvestmentAsset.query.get_or_404(asset_id)
    platform_id = asset.platform_id
    
    manual_types = ['Manual', 'Manual Earn', 'Staking', 'Lending']
    if asset.source_account_type not in manual_types:
        flash(f'Удаление актива {asset.ticker} ({asset.source_account_type}) запрещено, так как он синхронизируется автоматически.', 'warning')
        return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform_id))
    
    try:
        asset_ticker = asset.ticker
        db.session.delete(asset)
        db.session.commit()
        flash(f'Актив "{asset_ticker}" успешно удален.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Ошибка при удалении крипто-актива: {e}", exc_info=True)
        flash(f'Произошла ошибка при удалении актива: {e}', 'danger')
        
    return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform_id))

@main_bp.route('/platforms/<int:platform_id>/transactions/add_exchange', methods=['GET', 'POST'])
def ui_add_exchange_transaction_form(platform_id):
    """Обрабатывает добавление транзакции обмена для крипто-платформы."""
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    # Собираем все возможные активы для выбора в форме
    asset_tickers = {asset.ticker for asset in platform.assets.filter(InvestmentAsset.quantity > 0).all()}
    try:
        manual_balances = json.loads(platform.manual_earn_balances_json)
        asset_tickers.update(manual_balances.keys())
    except (json.JSONDecodeError, TypeError):
        pass
    asset_tickers.update(['USDT', 'USDC', 'BTC', 'ETH']) # Добавляем основные валюты
    available_assets = sorted(list(asset_tickers))

    if request.method == 'POST':
        try:
            asset1_ticker = request.form.get('asset1_ticker')
            asset1_amount = Decimal(request.form.get('asset1_amount'))
            asset2_ticker = request.form.get('asset2_ticker')
            asset2_amount = Decimal(request.form.get('asset2_amount'))
            fee_amount = Decimal(request.form.get('fee_amount', '0'))
            fee_currency = request.form.get('fee_currency')
            timestamp = datetime.strptime(request.form.get('timestamp'), '%Y-%m-%dT%H:%M').replace(tzinfo=timezone.utc)

            if not all([asset1_ticker, asset1_amount, asset2_ticker, asset2_amount]):
                raise ValueError("Все поля активов и их количества обязательны.")
            if asset1_ticker == asset2_ticker:
                raise ValueError("Активы для обмена должны быть разными.")

            new_tx = Transaction(
                platform_id=platform.id, timestamp=timestamp, type='exchange', raw_type='Manual Exchange',
                asset1_ticker=asset1_ticker, asset1_amount=asset1_amount,
                asset2_ticker=asset2_ticker, asset2_amount=asset2_amount,
                fee_amount=fee_amount, fee_currency=fee_currency if fee_amount > 0 else None,
                description=request.form.get('description')
            )
            db.session.add(new_tx)
            db.session.commit()
            flash('Транзакция обмена успешно добавлена.', 'success')
            return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform.id))
        except (ValueError, InvalidOperation) as e:
            db.session.rollback()
            flash(f'Ошибка в данных: {e}', 'danger')
    
    return render_template('add_exchange_transaction.html', platform=platform, available_assets=available_assets, now=datetime.now(timezone.utc), cancel_url=url_for('main.ui_investment_platform_detail', platform_id=platform.id))

@main_bp.route('/crypto-news')
def ui_crypto_news():
    """Отображает страницу с новостями и анализом трендов."""
    # Получаем анализ трендов для топ-10 активов портфеля
    news_trends, top_10_tickers = get_news_trends_for_portfolio()

    # Получаем общий список последних новостей
    try:
        # Загружаем последние 30 новостей без фильтра по категориям
        latest_news = get_crypto_news(limit=30)
    except Exception as e:
        current_app.logger.error(f"Не удалось загрузить общие новости: {e}")
        latest_news = []
        flash('Не удалось загрузить последние новости.', 'danger')

    return render_template(
        'crypto_news.html',
        latest_news=latest_news,
        news_trends=news_trends,
        top_10_tickers=top_10_tickers
    )

@main_bp.route('/securities-news')
def ui_securities_news():
    """Отображает страницу с новостями фондового рынка."""
    try:
        # Используем новую кэширующую функцию
        news_articles = get_securities_news(limit=50)
    except Exception as e:
        current_app.logger.error(f"Не удалось загрузить новости фондового рынка: {e}")
        flash("Не удалось загрузить новости. Попробуйте позже.", "danger")
        news_articles = []
    return render_template('securities_news.html', news_articles=news_articles)

# --- Routes that were copied from app.py ---
# (All other routes like /accounts, /transactions, /categories, /debts, /crypto-assets, etc. go here)
# IMPORTANT: Remember to change redirects like `url_for('index')` to `url_for('main.index')`

@main_bp.route('/accounts')
def ui_accounts():
    accounts = Account.query.order_by(Account.is_active.desc(), Account.name).all()
    return render_template('accounts.html', accounts=accounts)

@main_bp.route('/crypto-assets')
def ui_crypto_assets():
    # ИСПРАВЛЕНО: Добавляем joinedload для жадной загрузки связанных платформ.
    # Это решает ошибку DetachedInstanceError, когда сессия закрывается до того,
    # как происходит ленивая загрузка `asset.platform`.
    all_crypto_assets = InvestmentAsset.query.options(joinedload(InvestmentAsset.platform)).filter(
        InvestmentAsset.asset_type == 'crypto', InvestmentAsset.quantity > 0).all()
    currency_rates_to_rub = _get_currency_rates()

    if not all_crypto_assets:
        return render_template('crypto_assets_overview.html', assets=[], grand_total_rub=0, grand_total_usdt=0, platform_summary=[], chart_labels='[]', chart_data='[]', chart_history_labels='[]', chart_history_values='[]')

    aggregated_assets = defaultdict(lambda: {
        'total_quantity': Decimal(0),
        'total_value_rub': Decimal(0),
        'total_value_usdt': Decimal(0),
        'locations': [],
        'current_price': Decimal(0),
        'currency_of_price': 'USDT',
        'average_buy_price': Decimal(0)
    })

    platform_summary_agg = defaultdict(lambda: {'total_rub': Decimal(0), 'total_usdt': Decimal(0), 'id': None})
    grand_total_rub = Decimal(0)
    grand_total_usdt = Decimal(0)

    for asset in all_crypto_assets: # This loop populates aggregated_assets
        ticker = asset.ticker
        quantity = asset.quantity or Decimal(0)
        price = asset.current_price or Decimal(0)
        
        asset_value_usdt = quantity * price
        asset_value_rub = asset_value_usdt * currency_rates_to_rub.get('USDT', Decimal(1.0))

        agg = aggregated_assets[ticker]
        agg['total_quantity'] += quantity
        agg['total_value_usdt'] += asset_value_usdt
        agg['total_value_rub'] += asset_value_rub
        agg['current_price'] = price
        agg['currency_of_price'] = asset.currency_of_price or 'USDT'
        agg['locations'].append({
            'platform_name': asset.platform.name,
            'platform_id': asset.platform_id,
            'account_type': asset.source_account_type,
            'quantity': quantity
        })

        plat_summary = platform_summary_agg[asset.platform.name]
        plat_summary['id'] = asset.platform_id
        plat_summary['total_rub'] += asset_value_rub
        plat_summary['total_usdt'] += asset_value_usdt

        grand_total_rub += asset_value_rub
        grand_total_usdt += asset_value_usdt

    all_tickers = list(aggregated_assets.keys())
    
    price_changes = db.session.query(HistoricalPriceCache.ticker, HistoricalPriceCache.period, HistoricalPriceCache.change_percent).filter(HistoricalPriceCache.ticker.in_(all_tickers)).all()
    # Инициализируем словарь с пустыми значениями, чтобы избежать ошибок в шаблоне
    changes_by_ticker = defaultdict(lambda: {
        '24h': None, '7d': None, '30d': None, '90d': None, '180d': None, '365d': None
    })

    for ticker, period, change in price_changes:
        changes_by_ticker[ticker][period] = change

    buy_transactions = db.session.query(
        Transaction.asset1_ticker,
        func.sum(Transaction.asset2_amount).label('total_cost_usdt'),
        func.sum(Transaction.asset1_amount).label('total_quantity_bought')
    ).filter(
        Transaction.type == 'buy',
        Transaction.asset1_ticker.in_(all_tickers),
        Transaction.asset2_ticker == 'USDT'
    ).group_by(Transaction.asset1_ticker).all()

    avg_buy_prices = {
        ticker: total_cost / total_quantity if total_quantity > 0 else Decimal(0)
        for ticker, total_cost, total_quantity in buy_transactions
    }

    for ticker, data in aggregated_assets.items():
        data.update(changes_by_ticker[ticker])
        data['average_buy_price'] = avg_buy_prices.get(ticker, Decimal(0))

    final_assets_list = sorted(aggregated_assets.items(), key=lambda item: item[1]['total_value_rub'], reverse=True)
    platform_summary = sorted(platform_summary_agg.items(), key=lambda item: item[1]['total_rub'], reverse=True)

    # --- Подготовка данных для графиков ---
    # 1. Круговая диаграмма распределения активов
    chart_labels = [item[0] for item in final_assets_list]
    chart_data = [float(item[1]['total_value_rub']) for item in final_assets_list]

    # 2. Исторический график стоимости портфеля
    history_data = CryptoPortfolioHistory.query.order_by(CryptoPortfolioHistory.date.asc()).all()
    chart_history_labels = [h.date.strftime('%Y-%m-%d') for h in history_data]
    chart_history_values = [float(h.total_value_rub) for h in history_data]

    # --- Подготовка данных для новых аналитических графиков ---
    # 1. График PnL по активам
    assets_with_pnl = []
    for ticker, data in final_assets_list:
        if data['average_buy_price'] > 0:
            invested_usdt = data['total_quantity'] * data['average_buy_price']
            pnl_usdt = data['total_value_usdt'] - invested_usdt
            assets_with_pnl.append({'ticker': ticker, 'pnl': pnl_usdt})
    
    # Сортируем по PnL для наглядности
    sorted_pnl = sorted(assets_with_pnl, key=lambda x: x['pnl'], reverse=True)
    pnl_chart_labels = [item['ticker'] for item in sorted_pnl]
    pnl_chart_data = [float(item['pnl']) for item in sorted_pnl]

    # 2. Круговая диаграмма распределения по платформам
    platform_pie_labels = [item[0] for item in platform_summary]
    platform_pie_data = [float(item[1]['total_rub']) for item in platform_summary]

    # --- Новые данные для графиков производительности ---
    performance_chart_data, performance_chart_last_updated = get_performance_chart_data_from_cache()

    return render_template('crypto_assets_overview.html',
                           assets=final_assets_list, grand_total_rub=grand_total_rub, grand_total_usdt=grand_total_usdt,
                           platform_summary=platform_summary, chart_labels=json.dumps(chart_labels),
                           chart_data=json.dumps(chart_data), chart_history_labels=json.dumps(chart_history_labels),
                           chart_history_values=json.dumps(chart_history_values),
                           performance_chart_data=json.dumps(performance_chart_data),
                           performance_chart_last_updated=performance_chart_last_updated,
                           pnl_chart_labels=json.dumps(pnl_chart_labels),
                           pnl_chart_data=json.dumps(pnl_chart_data),
                           platform_pie_labels=json.dumps(platform_pie_labels),
                           platform_pie_data=json.dumps(platform_pie_data))

def _apply_crypto_transaction_filters_and_sort(query, args):
    """
    Применяет общие фильтры и сортировку из аргументов запроса к запросу транзакций.
    Фильтры по дате обрабатываются отдельно вызывающей стороной из-за разной логики обработки ошибок.
    """
    # Применяем фильтры
    if args.get('filter_type', 'all') != 'all':
        query = query.filter(Transaction.type == args.get('filter_type'))
    
    if args.get('filter_platform_id', 'all') != 'all':
        query = query.filter(Transaction.platform_id == int(args.get('filter_platform_id')))

    if args.get('filter_asset', 'all') != 'all':
        query = query.filter(
            or_(Transaction.asset1_ticker == args.get('filter_asset'), Transaction.asset2_ticker == args.get('filter_asset')))
    
    # Применяем сортировку
    sort_by = args.get('sort_by', 'timestamp')
    order = args.get('order', 'desc')
    sort_column = getattr(Transaction, sort_by, Transaction.timestamp)
    if order == 'desc':
        query = query.order_by(desc(sort_column))
    else:
        query = query.order_by(asc(sort_column))
        
    return query

@main_bp.route('/api/crypto-transactions')
def api_crypto_transactions():
    """
    API эндпоинт для получения следующих страниц транзакций в виде HTML.
    Используется для функционала "Загрузить еще".
    """
    page = request.args.get('page', 1, type=int)
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    transactions_query = Transaction.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'crypto_exchange'
    ).options(joinedload(Transaction.platform))

    # Применяем общие фильтры и сортировку
    transactions_query = _apply_crypto_transaction_filters_and_sort(transactions_query, request.args)

    try:
        if start_date_str:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            transactions_query = transactions_query.filter(Transaction.timestamp >= start_date)
        if end_date_str:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            transactions_query = transactions_query.filter(Transaction.timestamp < end_date + timedelta(days=1))
    except ValueError:
        pass # Ignore invalid date format in API calls

    pagination = transactions_query.paginate(page=page, per_page=150, error_out=False)
    transactions = pagination.items

    html = render_template('_crypto_transaction_rows.html', transactions=transactions)
    return jsonify({'html': html, 'has_next': pagination.has_next})

@main_bp.route('/crypto-transactions')
def ui_crypto_transactions():
    page = request.args.get('page', 1, type=int)
    start_date_str = request.args.get('start_date', '')
    end_date_str = request.args.get('end_date', '')

    # ИСПРАВЛЕНО: Базовый запрос теперь фильтрует транзакции, чтобы показывать только те,
    # которые относятся к платформам типа 'crypto_exchange'.
    transactions_query = Transaction.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'crypto_exchange'
    ).options(joinedload(Transaction.platform))

    # Применяем общие фильтры и сортировку
    transactions_query = _apply_crypto_transaction_filters_and_sort(transactions_query, request.args)

    # ИЗМЕНЕНО: Добавлен фильтр по диапазону дат
    try:
        if start_date_str:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            transactions_query = transactions_query.filter(Transaction.timestamp >= start_date)
        if end_date_str:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            # Включаем весь день до 23:59:59, фильтруя по "меньше следующего дня"
            transactions_query = transactions_query.filter(Transaction.timestamp < end_date + timedelta(days=1))
    except ValueError:
        flash('Неверный формат даты. Используйте ГГГГ-ММ-ДД.', 'danger')
        start_date_str, end_date_str = '', '' # Сбрасываем даты при ошибке

    # Paginate the results
    # Старая логика подсчета сводки возвращается на клиент, поэтому показываем больше данных.
    transactions_pagination = transactions_query.paginate(page=page, per_page=150, error_out=False)
    
    # ИСПРАВЛЕНО: Получаем типы транзакций и платформы только для криптобирж
    unique_transaction_types = [r[0] for r in db.session.query(Transaction.type).join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'crypto_exchange').distinct().order_by(Transaction.type).all()]
    available_platforms = InvestmentPlatform.query.filter_by(platform_type='crypto_exchange').order_by(InvestmentPlatform.name).all()
    
    # УЛУЧШЕНО: Получаем список всех уникальных активов для выпадающего списка фильтра.
    asset1_tickers = db.session.query(Transaction.asset1_ticker).join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'crypto_exchange',
        Transaction.asset1_ticker.isnot(None)
    ).distinct()
    asset2_tickers = db.session.query(Transaction.asset2_ticker).join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'crypto_exchange',
        Transaction.asset2_ticker.isnot(None)
    ).distinct()
    unique_assets = sorted(list(set([r[0] for r in asset1_tickers] + [r[0] for r in asset2_tickers])))

    return render_template('crypto_transactions.html', 
                           transactions=transactions_pagination.items,
                           pagination=transactions_pagination,
                           sort_by=request.args.get('sort_by', 'timestamp'), 
                           order=request.args.get('order', 'desc'), 
                           filter_type=request.args.get('filter_type', 'all'), 
                           filter_platform_id=request.args.get('filter_platform_id', 'all'),
                           filter_asset=request.args.get('filter_asset', 'all'),
                           start_date=start_date_str,
                           end_date=end_date_str,
                           unique_transaction_types=unique_transaction_types, 
                           platforms=available_platforms, 
                           unique_assets=unique_assets)

@main_bp.route('/crypto-assets/refresh-historical-data', methods=['POST'])
def ui_refresh_historical_data():
    success, message = refresh_crypto_price_change_data()
    if success:
        flash(message, 'success')
    else:
        flash(message, 'danger')
    return redirect(url_for('main.ui_crypto_assets'))

@main_bp.route('/analytics/refresh-performance-chart', methods=['POST'])
def ui_refresh_performance_chart():
    # ИЗМЕНЕНО: Выполняем задачу напрямую, а не в фоне.
    # Это будет долго, но бесплатно.
    flash('Началось обновление данных для графика производительности. Пожалуйста, подождите...', 'info')
    success, message = refresh_performance_chart_data()
    if success:
        flash(message, 'success')
    else:
        flash(message, 'danger')
    return redirect(url_for('main.ui_crypto_assets'))

@main_bp.route('/analytics/refresh-portfolio-history', methods=['POST'])
def ui_refresh_portfolio_history():
    # ИЗМЕНЕНО: Выполняем задачу напрямую.
    flash('Началось обновление истории портфеля. Это может занять несколько минут...', 'info')
    success, message = refresh_crypto_portfolio_history()
    if success:
        flash(message, 'success')
    else:
        flash(message, 'danger')
    return redirect(url_for('main.ui_crypto_assets'))

@main_bp.route('/analytics/refresh-securities-history', methods=['POST'])
def ui_refresh_securities_history():
    """Запускает пересчет истории стоимости портфеля ценных бумаг."""
    success, message = refresh_securities_portfolio_history()
    if success:
        flash(message, 'success')
    else:
        flash(message, 'danger')
    return redirect(url_for('main.index'))

# --- Placeholder routes for Banking section ---

@main_bp.route('/banking-transactions')
def ui_transactions():
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort_by', 'date') # noqa
    order = request.args.get('order', 'desc') # noqa
    filter_account_id = request.args.get('filter_account_id', 'all')
    filter_type = request.args.get('filter_type', 'all')

    query = BankingTransaction.query.options(
        joinedload(BankingTransaction.account_ref),
        joinedload(BankingTransaction.to_account_ref),
        joinedload(BankingTransaction.category_ref),
        # Eager load items and their categories to prevent N+1 queries in the template
        joinedload(BankingTransaction.items).joinedload(TransactionItem.category)
    )

    if filter_account_id != 'all':
        query = query.filter(BankingTransaction.account_id == int(filter_account_id))
    if filter_type != 'all':
        query = query.filter(BankingTransaction.transaction_type == filter_type)

    sort_column = getattr(BankingTransaction, sort_by, BankingTransaction.date)
    if order == 'desc':
        query = query.order_by(desc(sort_column))
    else:
        query = query.order_by(asc(sort_column))

    pagination = query.paginate(page=page, per_page=50, error_out=False)
    accounts = Account.query.filter_by(is_active=True).order_by(Account.name).all()
    unique_types = [r[0] for r in db.session.query(BankingTransaction.transaction_type).distinct().order_by(BankingTransaction.transaction_type).all()]

    return render_template('transactions.html', transactions=pagination.items, pagination=pagination, sort_by=sort_by, order=order, filter_account_id=filter_account_id, filter_type=filter_type, accounts=accounts, unique_types=unique_types)

@main_bp.route('/transactions/add', methods=['GET', 'POST'])
def ui_add_transaction_form():
    if request.method == 'POST':
        tx_type = request.form.get('transaction_type')
        try:
            account_id = int(request.form.get('account_id'))
            account = Account.query.get(account_id)
            if not account:
                raise ValueError("Счет не найден.")

            if tx_type == 'expense':
                amount = Decimal(request.form.get('amount', '0'))
                if amount <= 0: raise ValueError("Сумма должна быть положительной.")
                
                if account.account_type == 'credit':
                    account.balance += amount
                else:
                    account.balance -= amount

                new_tx = BankingTransaction(
                    transaction_type=tx_type,
                    amount=amount,
                    date=datetime.strptime(request.form.get('date'), '%Y-%m-%dT%H:%M'),
                    description=request.form.get('description'),
                    account_id=int(request.form.get('account_id')),
                    category_id=int(request.form.get('category_id')) if request.form.get('category_id') else None
                )
                db.session.add(new_tx)
            
            elif tx_type == 'income':
                amount = Decimal(request.form.get('amount', '0'))
                if amount <= 0: raise ValueError("Сумма должна быть положительной.")
                
                if account.account_type == 'credit':
                    account.balance -= amount
                else:
                    account.balance += amount

                new_tx = BankingTransaction(
                    transaction_type=tx_type,
                    amount=amount,
                    date=datetime.strptime(request.form.get('date'), '%Y-%m-%dT%H:%M'),
                    description=request.form.get('description'),
                    account_id=int(request.form.get('account_id')),
                    category_id=int(request.form.get('category_id')) if request.form.get('category_id') else None
                )
                db.session.add(new_tx)

            elif tx_type == 'transfer':
                amount = Decimal(request.form.get('amount', '0'))
                if amount <= 0: raise ValueError("Сумма должна быть положительной.")
                
                from_account_id = int(request.form.get('account_id'))
                to_account_id = int(request.form.get('to_account_id'))
                if from_account_id == to_account_id: raise ValueError("Счета для перевода должны отличаться.")

                from_account = account # Already fetched
                to_account = Account.query.get(to_account_id)
                if not to_account:
                    raise ValueError("Счет зачисления не найден.")

                if from_account.account_type == 'credit':
                    from_account.balance += amount
                else:
                    from_account.balance -= amount
                
                if to_account.account_type == 'credit':
                    to_account.balance -= amount
                else:
                    to_account.balance += amount

                new_tx = BankingTransaction(
                    transaction_type=tx_type,
                    amount=amount,
                    date=datetime.strptime(request.form.get('date'), '%Y-%m-%dT%H:%M'),
                    description=request.form.get('description'),
                    account_id=from_account_id,
                    to_account_id=to_account_id
                )
                db.session.add(new_tx)

            elif tx_type == 'exchange':
                from_amount = Decimal(request.form.get('amount', '0'))
                to_amount = Decimal(request.form.get('to_amount', '0'))
                if from_amount <= 0 or to_amount <= 0:
                    raise ValueError("Суммы для обмена должны быть положительными.")
                
                from_account_id = int(request.form.get('account_id'))
                to_account_id = int(request.form.get('to_account_id'))
                if from_account_id == to_account_id:
                    raise ValueError("Счета для обмена должны отличаться.")

                from_account = account # Already fetched
                to_account = Account.query.get(to_account_id)
                if not to_account: raise ValueError("Счет зачисления не найден.")
                from_account.balance -= from_amount
                to_account.balance += to_amount

                new_tx = BankingTransaction(
                    transaction_type=tx_type,
                    amount=from_amount,
                    to_amount=to_amount,
                    date=datetime.strptime(request.form.get('date'), '%Y-%m-%dT%H:%M'),
                    description=request.form.get('description'),
                    account_id=from_account_id,
                    to_account_id=to_account_id
                )
                db.session.add(new_tx)
            elif tx_type in ['purchase', 'manual_purchase']:
                item_names = request.form.getlist('item_name[]')
                item_quantities = request.form.getlist('item_quantity[]')
                item_prices = request.form.getlist('item_price[]')
                item_categories = request.form.getlist('item_category_id[]')

                if not item_names: raise ValueError("В покупке должен быть хотя бы один товар.")

                total_purchase_amount = sum(
                    Decimal(qty) * Decimal(price) for qty, price in zip(item_quantities, item_prices)
                )

                account.balance -= total_purchase_amount

                purchase_tx = BankingTransaction(
                    transaction_type='expense',
                    amount=total_purchase_amount,
                    date=datetime.strptime(request.form.get('date'), '%Y-%m-%dT%H:%M'),
                    description=request.form.get('description'),
                    merchant=request.form.get('merchant'),
                    account_id=int(request.form.get('account_id'))
                )
                db.session.add(purchase_tx)
                db.session.flush()

                for i in range(len(item_names)):
                    quantity = Decimal(item_quantities[i])
                    price = Decimal(item_prices[i])
                    category_id = int(item_categories[i]) if item_categories[i] else None
                    
                    item = TransactionItem(
                        name=item_names[i],
                        quantity=quantity,
                        price=price,
                        total=quantity * price,
                        transaction_id=purchase_tx.id,
                        category_id=category_id
                    )
                    db.session.add(item)
            
            else:
                raise ValueError("Неизвестный тип транзакции.")

            db.session.commit()
            flash('Транзакция успешно добавлена.', 'success')
            return redirect(url_for('main.ui_transactions'))

        except (ValueError, InvalidOperation) as e:
            db.session.rollback()
            flash(f'Ошибка в данных: {e}', 'danger')
    
    accounts = Account.query.filter_by(is_active=True).order_by(Account.name).all()
    expense_categories = Category.query.filter_by(type='expense', parent_id=None).order_by(Category.name).options(joinedload(Category.subcategories)).all()
    income_categories = Category.query.filter_by(type='income', parent_id=None).order_by(Category.name).options(joinedload(Category.subcategories)).all()
    
    return render_template(
        'add_transaction.html', 
        accounts=accounts, 
        expense_categories=expense_categories,
        income_categories=income_categories,
        now=datetime.now(timezone.utc)
    )

@main_bp.route('/transactions/<int:tx_id>/edit')
def ui_edit_transaction_form(tx_id):
    # Placeholder
    transaction = BankingTransaction.query.get_or_404(tx_id)
    accounts = Account.query.order_by(Account.name).all()
    categories = Category.query.order_by(Category.name).all()
    return render_template('edit_transaction.html', transaction=transaction, accounts=accounts, categories=categories)

@main_bp.route('/accounts/add', methods=['GET', 'POST'])
def add_account():
    """Обрабатывает добавление нового банковского счета (GET-форма, POST-создание)."""
    banks = Bank.query.order_by(Bank.name).all()
    if request.method == 'POST':
        try:
            new_account = Account()
            _populate_account_from_form(new_account, request.form)
            db.session.add(new_account)
            db.session.commit()
            flash(f'Счет "{new_account.name}" успешно создан.', 'success')
            return redirect(url_for('main.ui_accounts'))
        except (InvalidOperation, ValueError) as e:
            flash(f'Ошибка в данных: {e}', 'danger')
            return render_template('add_edit_account.html', form_action_url=url_for('main.add_account'), account=request.form, title="Добавить новый счет", banks=banks)
    # GET request
    return render_template('add_edit_account.html', form_action_url=url_for('main.add_account'), account=None, title="Добавить новый счет", banks=banks)

@main_bp.route('/accounts/<int:account_id>/edit', methods=['GET', 'POST'])
def ui_edit_account_form(account_id):
    account = Account.query.get_or_404(account_id)
    if request.method == 'POST':
        try:
            _populate_account_from_form(account, request.form)
            db.session.commit()
            flash(f'Счет "{account.name}" успешно обновлен.', 'success')
            return redirect(url_for('main.ui_accounts'))
        except (InvalidOperation, ValueError) as e:
            flash(f'Ошибка в данных: {e}', 'danger')
            # Передаем измененные данные формы обратно в шаблон
            form_data = request.form.to_dict()
            form_data['id'] = account_id # Сохраняем id для action в форме
            banks = Bank.query.order_by(Bank.name).all()
            return render_template('add_edit_account.html', form_action_url=url_for('main.ui_edit_account_form', account_id=account_id), account=form_data, title="Редактировать счет", banks=banks)
    banks = Bank.query.order_by(Bank.name).all()
    return render_template('add_edit_account.html', form_action_url=url_for('main.ui_edit_account_form', account_id=account_id), account=account, title="Редактировать счет", banks=banks)

@main_bp.route('/accounts/<int:account_id>/delete', methods=['POST'])
def ui_delete_account(account_id):
    account = Account.query.get_or_404(account_id)
    # Проверка, есть ли связанные транзакции
    if BankingTransaction.query.filter((BankingTransaction.account_id == account_id) | (BankingTransaction.to_account_id == account_id)).first():
        flash(f'Нельзя удалить счет "{account.name}", так как с ним связаны транзакции. Сначала удалите или перенесите транзакции.', 'danger')
        return redirect(url_for('main.ui_accounts'))
    
    db.session.delete(account)
    db.session.commit()
    flash(f'Счет "{account.name}" успешно удален.', 'success')
    return redirect(url_for('main.ui_accounts'))

@main_bp.route('/banks')
def ui_banks():
    """Отображает страницу со списком всех банков."""
    banks = Bank.query.order_by(Bank.name).all()
    return render_template('banks.html', banks=banks)

@main_bp.route('/banks/add', methods=['GET', 'POST'])
def ui_add_bank():
    """Обрабатывает добавление нового банка."""
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Название банка не может быть пустым.', 'danger')
        elif Bank.query.filter_by(name=name).first():
            flash(f'Банк с названием "{name}" уже существует.', 'danger')
        else:
            db.session.add(Bank(name=name))
            db.session.commit()
            flash(f'Банк "{name}" успешно добавлен.', 'success')
            return redirect(url_for('main.ui_banks'))
    return render_template('add_edit_bank.html', title="Добавить банк", bank=None)

@main_bp.route('/banks/<int:bank_id>/edit', methods=['GET', 'POST'])
def ui_edit_bank(bank_id):
    """Обрабатывает редактирование банка."""
    bank = Bank.query.get_or_404(bank_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Название банка не может быть пустым.', 'danger')
        elif Bank.query.filter(Bank.id != bank_id, Bank.name == name).first():
            flash(f'Банк с названием "{name}" уже существует.', 'danger')
        else:
            bank.name = name
            db.session.commit()
            flash('Название банка успешно обновлено.', 'success')
            return redirect(url_for('main.ui_banks'))
    return render_template('add_edit_bank.html', title="Редактировать банк", bank=bank)

@main_bp.route('/banks/<int:bank_id>/delete', methods=['POST'])
def ui_delete_bank(bank_id):
    """Обрабатывает удаление банка."""
    bank = Bank.query.get_or_404(bank_id)
    if bank.accounts.first():
        flash(f'Нельзя удалить банк "{bank.name}", так как с ним связаны счета. Сначала измените или удалите связанные счета.', 'danger')
        return redirect(url_for('main.ui_banks'))
    
    db.session.delete(bank)
    db.session.commit()
    flash(f'Банк "{bank.name}" успешно удален.', 'success')
    return redirect(url_for('main.ui_banks'))

@main_bp.route('/categories')
def ui_categories():
    expense_parents = Category.query.filter_by(type='expense', parent_id=None).order_by(Category.name).options(joinedload(Category.subcategories)).all()
    income_parents = Category.query.filter_by(type='income', parent_id=None).order_by(Category.name).all()
    return render_template('categories.html', expense_parents=expense_parents, income_parents=income_parents)

@main_bp.route('/categories/add', methods=['GET', 'POST'])
def ui_add_category_form():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        cat_type = request.form.get('type', 'expense').strip()
        parent_id = request.form.get('parent_id')
        if not name:
            flash('Название категории не может быть пустым.', 'danger')
        else:
            existing = Category.query.filter_by(name=name, type=cat_type).first()
            if existing:
                flash(f'Категория "{name}" с типом "{cat_type}" уже существует.', 'danger')
            else:
                new_category = Category(name=name, type=cat_type, parent_id=int(parent_id) if parent_id else None)
                db.session.add(new_category)
                db.session.commit()
                flash(f'Категория "{name}" успешно добавлена.', 'success')
                return redirect(url_for('main.ui_categories'))
    
    parent_categories = Category.query.filter_by(parent_id=None).order_by(Category.type, Category.name).all()
    return render_template('add_edit_category.html', title="Добавить категорию", category=None, parent_categories=parent_categories)

@main_bp.route('/categories/<int:category_id>/edit', methods=['GET', 'POST'])
def ui_edit_category_form(category_id):
    category = Category.query.get_or_404(category_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        cat_type = request.form.get('type', 'expense').strip()
        parent_id = request.form.get('parent_id')
        if not name:
            flash('Название категории не может быть пустым.', 'danger')
        else:
            existing = Category.query.filter(
                Category.id != category_id,
                Category.name == name,
                Category.type == cat_type
            ).first()
            if existing:
                flash(f'Категория "{name}" с типом "{cat_type}" уже существует.', 'danger')
            else:
                category.name = name
                category.type = cat_type
                category.parent_id = int(parent_id) if parent_id else None
                db.session.commit()
                flash(f'Категория "{name}" успешно обновлена.', 'success')
                return redirect(url_for('main.ui_categories'))
    parent_categories = Category.query.filter(Category.parent_id.is_(None), Category.id != category_id).order_by(Category.type, Category.name).all()
    return render_template('add_edit_category.html', title="Редактировать категорию", category=category, parent_categories=parent_categories)

@main_bp.route('/categories/<int:category_id>/delete', methods=['POST'])
def ui_delete_category(category_id):
    category = Category.query.get_or_404(category_id)
    if BankingTransaction.query.filter_by(category_id=category_id).first() or \
       TransactionItem.query.filter_by(category_id=category_id).first():
        flash(f'Нельзя удалить категорию "{category.name}", так как она используется в транзакциях.', 'danger')
        return redirect(url_for('main.ui_categories'))
    
    db.session.delete(category)
    db.session.commit()
    flash(f'Категория "{category.name}" успешно удалена.', 'success')
    return redirect(url_for('main.ui_categories'))

@main_bp.route('/debts')
def ui_debts():
    i_owe_list = Debt.query.filter_by(debt_type='i_owe').order_by(Debt.status, Debt.due_date.asc()).all()
    owed_to_me_list = Debt.query.filter_by(debt_type='owed_to_me').order_by(Debt.status, Debt.due_date.asc()).all()

    i_owe_total = sum(d.initial_amount - d.repaid_amount for d in i_owe_list if d.status == 'active')
    owed_to_me_total = sum(d.initial_amount - d.repaid_amount for d in owed_to_me_list if d.status == 'active')

    return render_template('debts.html', 
                           i_owe_list=i_owe_list, 
                           owed_to_me_list=owed_to_me_list,
                           i_owe_total=i_owe_total,
                           owed_to_me_total=owed_to_me_total)

@main_bp.route('/debts/add', methods=['GET', 'POST'])
def add_debt():
    if request.method == 'POST':
        try:
            initial_amount = Decimal(request.form.get('initial_amount', '0'))
            if initial_amount <= 0:
                raise ValueError("Сумма долга должна быть положительной.")

            new_debt = Debt(
                debt_type=request.form['debt_type'],
                counterparty=request.form['counterparty'],
                initial_amount=initial_amount,
                currency=request.form['currency'],
                description=request.form.get('notes'),
                status='active',
                repaid_amount=Decimal(0)
            )
            due_date_str = request.form.get('due_date')
            if due_date_str:
                new_debt.due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            
            db.session.add(new_debt)
            db.session.commit()
            flash('Долг успешно добавлен.', 'success')
            return redirect(url_for('main.ui_debts'))
        except (ValueError, InvalidOperation) as e:
            flash(f'Ошибка в данных: {e}', 'danger')
            return render_template('add_edit_debt.html', title="Добавить долг", debt=request.form)
    
    return render_template('add_edit_debt.html', title="Добавить долг", debt=None)

@main_bp.route('/debts/<int:debt_id>/edit', methods=['GET', 'POST'])
def edit_debt(debt_id):
    debt = Debt.query.get_or_404(debt_id)
    if request.method == 'POST':
        try:
            initial_amount = Decimal(request.form.get('initial_amount', '0'))
            if initial_amount <= 0:
                raise ValueError("Сумма долга должна быть положительной.")
            
            debt.debt_type = request.form['debt_type']
            debt.counterparty = request.form['counterparty']
            debt.initial_amount = initial_amount
            debt.currency = request.form['currency']
            debt.description = request.form.get('notes')
            debt.status = request.form.get('status', 'active')
            
            due_date_str = request.form.get('due_date')
            debt.due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date() if due_date_str else None
            
            db.session.commit()
            flash('Долг успешно обновлен.', 'success')
            return redirect(url_for('main.ui_debts'))
        except (ValueError, InvalidOperation) as e:
            flash(f'Ошибка в данных: {e}', 'danger')
            return render_template('add_edit_debt.html', title="Редактировать долг", debt=debt)

    return render_template('add_edit_debt.html', title="Редактировать долг", debt=debt)

@main_bp.route('/debts/<int:debt_id>/delete', methods=['POST'])
def delete_debt(debt_id):
    debt = Debt.query.get_or_404(debt_id)
    if debt.repayments.first():
        flash('Нельзя удалить долг, по которому есть операции погашения. Сначала удалите связанные банковские транзакции.', 'danger')
        return redirect(url_for('main.ui_debts'))
    
    db.session.delete(debt)
    db.session.commit()
    flash(f'Долг для "{debt.counterparty}" успешно удален.', 'success')
    return redirect(url_for('main.ui_debts'))

@main_bp.route('/debts/<int:debt_id>/repay', methods=['GET', 'POST'])
def repay_debt(debt_id):
    debt = Debt.query.get_or_404(debt_id)
    remaining_amount = debt.initial_amount - debt.repaid_amount
    
    if request.method == 'POST':
        # ... (логика обработки POST-запроса будет добавлена в следующем шаге)
        pass

    accounts = Account.query.filter_by(is_active=True, currency=debt.currency).order_by(Account.name).all()
    if not accounts and debt.status == 'active':
        flash(f'Не найден ни один активный счет в валюте {debt.currency} для выполнения операции.', 'warning')
    
    return render_template('repay_debt.html', debt=debt, remaining_amount=remaining_amount, accounts=accounts, now=datetime.now(timezone.utc))

@main_bp.route('/analytics')
def ui_analytics_overview():
    flash('Раздел "Аналитика" еще не реализован.', 'info')
    return redirect(url_for('main.index'))

@main_bp.route('/cashback-rules')
def ui_cashback_rules():
    flash('Раздел "Правила кэшбэка" еще не реализован.', 'info')
    return redirect(url_for('main.index'))

@main_bp.route('/cashback-rules/add')
def ui_add_cashback_rule_form():
    flash('Форма добавления правила кэшбэка еще не реализована.', 'info')
    return redirect(url_for('main.index'))

@main_bp.route('/cashback-rules/<int:rule_id>/edit')
def ui_edit_cashback_rule_form(rule_id):
    flash(f'Форма редактирования правила кэшбэка {rule_id} еще не реализована.', 'info')
    return redirect(url_for('main.index'))
