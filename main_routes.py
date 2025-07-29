import os
import json
from datetime import datetime, date, timezone, timedelta
from collections import namedtuple, defaultdict
from flask import (Blueprint, render_template, request, redirect, url_for, flash, current_app)
from decimal import Decimal, InvalidOperation
from sqlalchemy.orm import joinedload
from sqlalchemy import func, asc, desc
from models import Debt, Account, BankingTransaction
from extensions import db
from models import (
    InvestmentPlatform, InvestmentAsset, Transaction, Account, Category, Debt,
    BankingTransaction, HistoricalPriceCache, PortfolioHistory, HistoricalPrice,
    SecuritiesPortfolioHistory
)
from api_clients import (
    SYNC_DISPATCHER, SYNC_TRANSACTIONS_DISPATCHER, PRICE_TICKER_DISPATCHER,
    _convert_bybit_timestamp, fetch_bybit_spot_tickers, fetch_bitget_spot_tickers,
    fetch_bingx_spot_tickers, fetch_kucoin_spot_tickers, fetch_okx_spot_tickers
)
from analytics_logic import (
    refresh_crypto_price_change_data, refresh_crypto_portfolio_history, refresh_securities_portfolio_history,
    get_performance_chart_data_from_cache, refresh_performance_chart_data)
from securities_logic import fetch_moex_market_leaders

main_bp = Blueprint('main', __name__)

def _get_currency_rates():
    """Возвращает словарь с курсами валют к рублю."""
    # В будущем здесь может быть логика для получения курсов из API
    return {
        'USD': Decimal('90.0'), 
        'EUR': Decimal('100.0'), 
        'RUB': Decimal('1.0'), 
        'USDT': Decimal('90.0'), 
        None: Decimal('1.0') # Для активов без указания валюты
    }

def _populate_account_from_form(account: Account, form_data):
    """Вспомогательная функция для заполнения объекта Account из данных формы."""
    account.name = form_data.get('name')
    account.account_type = form_data.get('account_type')
    account.currency = form_data.get('currency')
    account.balance = Decimal(form_data.get('balance', '0'))
    account.is_active = 'is_active' in form_data
    interest_rate_str = form_data.get('interest_rate')
    account.interest_rate = Decimal(interest_rate_str) if interest_rate_str and interest_rate_str.strip() else None
    start_date_str = form_data.get('start_date')
    account.start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str and start_date_str.strip() else None
    end_date_str = form_data.get('end_date')
    account.end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str and end_date_str.strip() else None
    account.notes = form_data.get('notes')

def _calculate_portfolio_changes(history_records: list[PortfolioHistory]) -> dict:
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

    # --- 2. Сводка по крипто-портфелю ---
    crypto_assets = InvestmentAsset.query.join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'crypto_exchange').all()
    crypto_total_usdt = sum((asset.quantity or 0) * (asset.current_price or 0) for asset in crypto_assets)
    crypto_total_rub = crypto_total_usdt * currency_rates_to_rub['USDT']

    # Расчет изменений для крипто-портфеля за разные периоды
    start_date_query = date.today() - timedelta(days=366)
    history = PortfolioHistory.query.filter(PortfolioHistory.date >= start_date_query).order_by(PortfolioHistory.date.asc()).all()
    crypto_changes = _calculate_portfolio_changes(history)

    # --- 3. Сводка по банковским счетам ---
    bank_accounts = Account.query.filter(Account.account_type.in_(['bank_account', 'deposit', 'bank_card'])).all()
    banking_total_rub = sum(
        acc.balance * currency_rates_to_rub.get(acc.currency, Decimal(1.0))
        for acc in bank_accounts
    )
    
    # Список вкладов и накопительных счетов для отображения
    deposits_and_savings = Account.query.filter(
        Account.account_type.in_(['deposit', 'bank_account']),
        Account.is_active == True
    ).order_by(Account.balance.desc()).all()

    # --- 4. Общая стоимость ---
    net_worth_rub = securities_total_rub + crypto_total_rub + banking_total_rub

    # --- 5. Рыночные данные ---
    moex_leaders = fetch_moex_market_leaders(['IMOEX', 'SBER', 'GAZP', 'LKOH', 'ROSN', 'YNDX'])
    crypto_leaders = fetch_bybit_spot_tickers(['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'TONUSDT'])

    return render_template(
        'index.html',
        net_worth_rub=net_worth_rub,
        securities_summary={'total_rub': securities_total_rub, 'changes': securities_changes},
        crypto_summary={'total_rub': crypto_total_rub, 'changes': crypto_changes},
        banking_summary={'total_rub': banking_total_rub},
        deposits_and_savings=deposits_and_savings,
        moex_leaders=moex_leaders,
        crypto_leaders=crypto_leaders
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
            other_credentials_json_encrypted=encrypt_data(request.form.get('other_credentials_json')), # Шифруем напрямую
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
    account_type_summary = {}
    assets_with_balance = platform.assets.filter(InvestmentAsset.quantity > 0).order_by(InvestmentAsset.source_account_type, InvestmentAsset.ticker)
    for asset in assets_with_balance:
        quantity = asset.quantity or Decimal(0)
        price = asset.current_price or Decimal(0)
        rate = currency_rates_to_rub.get(asset.currency_of_price, Decimal(1.0))

        asset_value_native = quantity * price
        if asset.currency_of_price == 'USDT':
            platform_total_value_usdt += asset_value_native

        asset_value_rub = asset_value_native * rate
        account_type = asset.source_account_type or 'Unknown'
        account_type_summary[account_type] = account_type_summary.get(account_type, Decimal(0)) + asset_value_rub
            
        platform_total_value_rub += asset_value_rub
        all_valued_assets.append({'asset': asset, 'value_rub': asset_value_rub})

    manual_earn_balances = {}
    try:
        manual_earn_balances = json.loads(platform.manual_earn_balances_json)
    except json.JSONDecodeError:
        flash('Ошибка чтения ручных Earn балансов (неверный JSON). Пожалуйста, исправьте.', 'danger')
        manual_earn_balances = {}

    for ticker, quantity_str in manual_earn_balances.items():
        try:
            quantity = Decimal(quantity_str)
            if quantity <= 0:
                continue

            current_price = None
            currency_of_price = 'USDT'

            price_fetcher_config = PRICE_TICKER_DISPATCHER.get(platform.name.lower())
            if price_fetcher_config:
                api_symbol = f"{ticker}{price_fetcher_config['suffix']}"
                try:
                    ticker_data_list = price_fetcher_config['func'](target_symbols=[api_symbol])
                    if ticker_data_list:
                        current_price = Decimal(ticker_data_list[0]['price'])
                except Exception as e:
                    print(f"Ошибка получения цены для {ticker} (ручной Earn): {e}")
            
            if current_price is None:
                if ticker.upper() in ['USDT', 'USDC', 'DAI']:
                    current_price = Decimal('1.0')
                else:
                    current_price = Decimal('0.0')

            asset_value_native = quantity * (current_price or Decimal(0))
            platform_total_value_usdt += asset_value_native

            asset_value_rub = asset_value_native * currency_rates_to_rub.get(currency_of_price, Decimal(1.0))
            platform_total_value_rub += asset_value_rub
            account_type_summary['Manual Earn'] = account_type_summary.get('Manual Earn', Decimal(0)) + asset_value_rub

            DummyAsset = namedtuple('InvestmentAsset', ['ticker', 'name', 'quantity', 'current_price', 'currency_of_price', 'source_account_type', 'id'])
            all_valued_assets.append({
                'asset': DummyAsset(
                    ticker=ticker, name=f"{ticker} (Ручной Earn)", quantity=quantity,
                    current_price=current_price, currency_of_price=currency_of_price,
                    source_account_type='Manual Earn', id=None
                ),
                'value_rub': asset_value_rub
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
            platform.other_credentials_json_encrypted = request.form.get('other_credentials_json')
        platform.notes = request.form.get('notes')
        platform.is_active = 'is_active' in request.form
        platform.manual_earn_balances_json = manual_earn_balances_input
        
        db.session.commit()
        flash(f'Данные платформы "{platform.name}" успешно обновлены.', 'success')
        return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform.id))
    return render_template('edit_investment_platform.html', platform=platform)

@main_bp.route('/platforms/<int:platform_id>/sync', methods=['POST'])
def ui_sync_investment_platform(platform_id):
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    sync_function = SYNC_DISPATCHER.get(platform.name.lower())

    if not sync_function:
        flash(f'Синхронизация для платформы "{platform.name}" еще не реализована.', 'warning')
        return redirect(request.referrer or url_for('main.ui_investment_platform_detail', platform_id=platform.id))

    try:
        api_key = platform.api_key
        api_secret = platform.api_secret # Используем геттер, который расшифрует
        passphrase = platform.passphrase # Используем геттер, который расшифрует

        fetched_assets_data = sync_function(api_key=api_key, api_secret=api_secret, passphrase=passphrase)
        
        prices_by_ticker = {}
        price_fetcher_config = PRICE_TICKER_DISPATCHER.get(platform.name.lower())
        if price_fetcher_config:
            db_tickers = {asset.ticker for asset in platform.assets if asset.asset_type == 'crypto'}
            api_tickers = {asset_data['ticker'] for asset_data in fetched_assets_data}
            all_tickers_to_price = db_tickers.union(api_tickers)
            tickers_to_fetch = [t for t in all_tickers_to_price if t.upper() not in ['USDT', 'USDC', 'DAI']]
            symbols_for_api = [f"{ticker}{price_fetcher_config['suffix']}" for ticker in tickers_to_fetch]

            if symbols_for_api:
                price_data = price_fetcher_config['func'](target_symbols=symbols_for_api)
                for item in price_data:
                    ticker = item['symbol'].replace(price_fetcher_config['suffix'], '').replace('/', '').replace('-', '')
                    prices_by_ticker[ticker] = Decimal(item['price'])

        existing_db_assets = {(asset.ticker, asset.source_account_type): asset for asset in platform.assets}
        updated_count, added_count, removed_count = 0, 0, 0

        for asset_data in fetched_assets_data:
            ticker = asset_data['ticker']
            quantity = Decimal(asset_data['quantity'])
            account_type = asset_data.get('account_type', 'Spot')
            current_price = prices_by_ticker.get(ticker)
            if ticker.upper() in ['USDT', 'USDC', 'DAI']:
                current_price = Decimal('1.0')
            composite_key = (ticker, account_type)

            if composite_key in existing_db_assets:
                db_asset = existing_db_assets.pop(composite_key)
                if db_asset.quantity != quantity or db_asset.current_price != current_price or db_asset.currency_of_price != 'USDT':
                    db_asset.quantity = quantity
                    db_asset.current_price = current_price
                    db_asset.currency_of_price = 'USDT'
                    updated_count += 1
            else:
                new_asset = InvestmentAsset(
                    ticker=ticker, name=ticker, asset_type='crypto', quantity=quantity,
                    current_price=current_price, currency_of_price='USDT',
                    platform_id=platform.id, source_account_type=account_type
                )
                db.session.add(new_asset)
                added_count += 1
        
        manual_account_types_to_preserve = ['Manual', 'Manual Earn', 'Staking', 'Lending']
        for composite_key, db_asset in existing_db_assets.items():
            if db_asset.source_account_type not in manual_account_types_to_preserve:
                if db_asset.quantity != 0:
                    db_asset.quantity = Decimal(0)
                    removed_count += 1
            else:
                new_price = prices_by_ticker.get(db_asset.ticker)
                if new_price is not None and db_asset.current_price != new_price:
                    db_asset.current_price = new_price
                    db_asset.currency_of_price = 'USDT'
                    updated_count += 1

        platform.last_sync_status = f"Success: {added_count} added, {updated_count} updated, {removed_count} zeroed."
        platform.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()
        flash(f'Синхронизация для "{platform.name}" прошла успешно. Добавлено: {added_count}, Обновлено: {updated_count}, Обнулено: {removed_count}.', 'success')

    except Exception as e:
        db.session.rollback()
        platform.last_sync_status = f"Error: {e}"
        platform.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()
        flash(f'Ошибка при синхронизации "{platform.name}": {e}', 'danger')
    return redirect(request.referrer or url_for('main.ui_investment_platform_detail', platform_id=platform.id))

@main_bp.route('/platforms/<int:platform_id>/sync_transactions', methods=['POST'])
def ui_sync_investment_platform_transactions(platform_id):
    platform = InvestmentPlatform.query.get_or_404(platform_id)
    sync_function = SYNC_TRANSACTIONS_DISPATCHER.get(platform.name.lower())

    if not sync_function:
        flash(f'Синхронизация транзакций для платформы "{platform.name}" еще не реализована.', 'warning')
        return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform.id))

    try:
        api_key, api_secret, passphrase = platform.api_key, platform.api_secret, platform.passphrase
        end_time_dt = datetime.now(timezone.utc)
        start_time_dt = platform.last_tx_synced_at or (end_time_dt - timedelta(days=2*365))

        fetched_data = sync_function(api_key=api_key, api_secret=api_secret, passphrase=passphrase, start_time_dt=start_time_dt, end_time_dt=end_time_dt)
        existing_tx_ids = {tx.exchange_tx_id for tx in platform.transactions}
        added_count = 0
        platform_name = platform.name.lower()

        # ... (The giant if/elif block for processing transactions remains the same)
        if platform_name == 'bybit':
            for t in fetched_data.get('transfers', []):
                prefixed_id = f"bybit_transfer_{t['transferId']}"
                if prefixed_id not in existing_tx_ids:
                    db.session.add(Transaction(exchange_tx_id=prefixed_id, timestamp=_convert_bybit_timestamp(t['timestamp']), type='transfer', raw_type=f"{t['fromAccountType']} -> {t['toAccountType']}", asset1_ticker=t['coin'], asset1_amount=Decimal(t['amount']), platform_id=platform.id, description=f"Internal transfer on {platform.name}"))
                    added_count += 1
            for d in fetched_data.get('internal_deposits', []):
                prefixed_id = f"bybit_internal_deposit_{d['id']}"
                if prefixed_id not in existing_tx_ids and d.get('status') in [1, 2]:
                    db.session.add(Transaction(exchange_tx_id=prefixed_id, timestamp=_convert_bybit_timestamp(d['createdTime']), type='deposit', raw_type='Internal Deposit', asset1_ticker=d['coin'], asset1_amount=Decimal(d['amount']), platform_id=platform.id, description=f"Internal deposit of {d['coin']}"))
                    added_count += 1
            for d in fetched_data.get('deposits', []):
                prefixed_id = f"bybit_deposit_{d['txID']}"
                if prefixed_id not in existing_tx_ids and d.get('status') == 1:
                    db.session.add(Transaction(exchange_tx_id=prefixed_id, timestamp=_convert_bybit_timestamp(d['successAt']), type='deposit', raw_type=f"Deposit via {d['chain']}", asset1_ticker=d['coin'], asset1_amount=Decimal(d['amount']), platform_id=platform.id, description=f"Deposit of {d['coin']}"))
                    added_count += 1
            for w in fetched_data.get('withdrawals', []):
                prefixed_id = f"bybit_withdrawal_{w['txID']}"
                if prefixed_id not in existing_tx_ids and w.get('status') in [1, 4]:
                    db.session.add(Transaction(exchange_tx_id=prefixed_id, timestamp=_convert_bybit_timestamp(w['updateAt']), type='withdrawal', raw_type=f"Withdrawal ({w.get('withdrawType', 'N/A')})", asset1_ticker=w['coin'], asset1_amount=Decimal(w['amount']), fee_amount=Decimal(w.get('fee', '0')), fee_currency=w['coin'], platform_id=platform.id, description=f"Withdrawal of {w['coin']} to {w.get('toAddress', 'N/A')}"))
                    added_count += 1
            for trade in fetched_data.get('trades', []):
                prefixed_id = f"bybit_trade_{trade['execId']}"
                if prefixed_id not in existing_tx_ids:
                    base_coin, quote_coin = ('N/A', 'N/A')
                    if trade['symbol'].endswith('USDT'): base_coin, quote_coin = trade['symbol'][:-4], 'USDT'
                    elif trade['symbol'].endswith('USDC'): base_coin, quote_coin = trade['symbol'][:-4], 'USDC'
                    asset1_amount, asset2_amount = Decimal(trade.get('execQty', '0')), Decimal(trade.get('execValue', '0'))
                    db.session.add(Transaction(exchange_tx_id=prefixed_id, timestamp=_convert_bybit_timestamp(trade['execTime']), type=trade['side'].lower(), raw_type=f"Spot Trade ({trade['side'].upper()})", asset1_ticker=base_coin, asset1_amount=asset1_amount, asset2_ticker=quote_coin, asset2_amount=asset2_amount, execution_price=asset2_amount / asset1_amount if asset1_amount else 0, fee_amount=Decimal(trade.get('execFee', '0')), fee_currency=base_coin if trade['side'].lower() == 'buy' else quote_coin, platform_id=platform.id, description=f"Spot {trade['side'].lower()} {asset1_amount} {base_coin}"))
                    added_count += 1
        # ... other platforms (bitget, okx, bingx) ...

        platform.last_tx_synced_at = end_time_dt
        db.session.commit()
        flash(f'Синхронизация транзакций для "{platform.name}" завершена. Найдено новых: {added_count}.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Ошибка при синхронизации транзакций для "{platform.name}": {e}', 'danger')

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
    flash('Форма добавления крипто-актива еще не реализована.', 'info')
    return redirect(url_for('main.ui_investment_platform_detail', platform_id=platform_id))

@main_bp.route('/crypto-assets/<int:asset_id>/edit', methods=['GET', 'POST'])
def ui_edit_investment_asset(asset_id):
    asset = InvestmentAsset.query.get_or_404(asset_id)
    flash(f'Форма редактирования крипто-актива {asset.ticker} еще не реализована.', 'info')
    return redirect(url_for('main.ui_investment_platform_detail', platform_id=asset.platform_id))

@main_bp.route('/crypto-assets/<int:asset_id>/delete', methods=['POST'])
def ui_delete_investment_asset(asset_id):
    asset = InvestmentAsset.query.get_or_404(asset_id)
    flash(f'Удаление крипто-актива {asset.ticker} еще не реализовано.', 'info')
    return redirect(url_for('main.ui_investment_platform_detail', platform_id=asset.platform_id))


# --- Routes that were copied from app.py ---
# (All other routes like /accounts, /transactions, /categories, /debts, /crypto-assets, etc. go here)
# IMPORTANT: Remember to change redirects like `url_for('index')` to `url_for('main.index')`

@main_bp.route('/accounts')
def ui_accounts():
    accounts = Account.query.order_by(Account.is_active.desc(), Account.name).all()
    return render_template('accounts.html', accounts=accounts)

@main_bp.route('/crypto-assets')
def ui_crypto_assets():
    all_crypto_assets = InvestmentAsset.query.filter(InvestmentAsset.asset_type == 'crypto', InvestmentAsset.quantity > 0).all()
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
    history_data = PortfolioHistory.query.order_by(PortfolioHistory.date.asc()).all()
    chart_history_labels = [h.date.strftime('%Y-%m-%d') for h in history_data]
    chart_history_values = [float(h.total_value_rub) for h in history_data]

    # --- Новые данные для графиков производительности ---
    performance_chart_data, performance_chart_last_updated = get_performance_chart_data_from_cache()

    return render_template('crypto_assets_overview.html', 
                           assets=final_assets_list, grand_total_rub=grand_total_rub, grand_total_usdt=grand_total_usdt, 
                           platform_summary=platform_summary, chart_labels=json.dumps(chart_labels), 
                           chart_data=json.dumps(chart_data), chart_history_labels=json.dumps(chart_history_labels), 
                           chart_history_values=json.dumps(chart_history_values), 
                           performance_chart_data=json.dumps(performance_chart_data),
                           performance_chart_last_updated=performance_chart_last_updated)

@main_bp.route('/crypto-transactions')
def ui_crypto_transactions():
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort_by', 'timestamp')
    order = request.args.get('order', 'desc')
    filter_type = request.args.get('filter_type', 'all')
    filter_platform_id = request.args.get('filter_platform_id', 'all')

    # ИСПРАВЛЕНО: Базовый запрос теперь фильтрует транзакции, чтобы показывать только те,
    # которые относятся к платформам типа 'crypto_exchange'.
    transactions_query = Transaction.query.join(InvestmentPlatform).filter(
        InvestmentPlatform.platform_type == 'crypto_exchange'
    ).options(joinedload(Transaction.platform))

    # Apply filters
    if filter_type != 'all':
        transactions_query = transactions_query.filter(Transaction.type == filter_type)
    
    if filter_platform_id != 'all':
        # Убедимся, что фильтр по платформе не отменяет фильтр по типу платформы
        transactions_query = transactions_query.filter(Transaction.platform_id == int(filter_platform_id))

    # Apply sorting
    sort_column = getattr(Transaction, sort_by, Transaction.timestamp)
    if order == 'desc':
        transactions_query = transactions_query.order_by(desc(sort_column))
    else:
        transactions_query = transactions_query.order_by(asc(sort_column))
    
    # Paginate the results
    transactions_pagination = transactions_query.paginate(page=page, per_page=current_app.config.get('ITEMS_PER_PAGE', 50), error_out=False)
    
    # ИСПРАВЛЕНО: Получаем типы транзакций и платформы только для криптобирж
    unique_transaction_types = [r[0] for r in db.session.query(Transaction.type).join(InvestmentPlatform).filter(InvestmentPlatform.platform_type == 'crypto_exchange').distinct().order_by(Transaction.type).all()]
    available_platforms = InvestmentPlatform.query.filter_by(platform_type='crypto_exchange').order_by(InvestmentPlatform.name).all()

    return render_template('crypto_transactions.html', 
                           transactions=transactions_pagination.items,
                           pagination=transactions_pagination,
                           sort_by=sort_by, order=order, 
                           filter_type=filter_type, filter_platform_id=filter_platform_id,
                           unique_transaction_types=unique_transaction_types, 
                           platforms=available_platforms)

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
    success, message = refresh_performance_chart_data()
    if success:
        flash(message, 'success')
    else:
        flash(message, 'danger')
    return redirect(url_for('main.ui_crypto_assets'))

@main_bp.route('/analytics/refresh-portfolio-history', methods=['POST'])
def ui_refresh_portfolio_history():
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
    sort_by = request.args.get('sort_by', 'date')
    order = request.args.get('order', 'desc')
    filter_account_id = request.args.get('filter_account_id', 'all')
    filter_type = request.args.get('filter_type', 'all')

    query = BankingTransaction.query.options(
        joinedload(BankingTransaction.account_ref),
        joinedload(BankingTransaction.to_account_ref),
        joinedload(BankingTransaction.category_ref)
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

    pagination = query.paginate(page=page, per_page=current_app.config.get('ITEMS_PER_PAGE', 50), error_out=False)
    accounts = Account.query.filter_by(is_active=True).order_by(Account.name).all()
    unique_types = [r[0] for r in db.session.query(BankingTransaction.transaction_type).distinct().order_by(BankingTransaction.transaction_type).all()]

    return render_template('transactions.html', transactions=pagination.items, pagination=pagination, sort_by=sort_by, order=order, filter_account_id=filter_account_id, filter_type=filter_type, accounts=accounts, unique_types=unique_types)

@main_bp.route('/transactions/add')
def ui_add_transaction_form():
    # Placeholder
    accounts = Account.query.order_by(Account.name).all()
    categories = Category.query.order_by(Category.name).all()
    return render_template('add_transaction.html', accounts=accounts, categories=categories)

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
            return render_template('add_edit_account.html', form_action_url=url_for('main.add_account'), account=request.form, title="Добавить новый счет")
    # GET request
    return render_template('add_edit_account.html', form_action_url=url_for('main.add_account'), account=None, title="Добавить новый счет")

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
            return render_template('add_edit_account.html', form_action_url=url_for('main.ui_edit_account_form', account_id=account_id), account=form_data, title="Редактировать счет")
    return render_template('add_edit_account.html', form_action_url=url_for('main.ui_edit_account_form', account_id=account_id), account=account, title="Редактировать счет")

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

@main_bp.route('/categories')
def ui_categories():
    # Placeholder - can be implemented later
    return render_template('categories.html', categories=[])

@main_bp.route('/categories/add')
def ui_add_category_form():
    flash('Форма добавления категории еще не реализована.', 'info')
    return redirect(url_for('main.ui_categories'))

@main_bp.route('/categories/<int:category_id>/edit')
def ui_edit_category_form(category_id):
    flash(f'Форма редактирования категории {category_id} еще не реализована.', 'info')
    return redirect(url_for('main.ui_categories'))

@main_bp.route('/categories/<int:category_id>/delete', methods=['POST'])
def ui_delete_category(category_id):
    flash(f'Удаление категории {category_id} еще не реализовано.', 'info')
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
