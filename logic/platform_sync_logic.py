from flask import current_app
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json

from models import InvestmentPlatform, InvestmentAsset, Transaction
from extensions import db
from api_clients import (
    SYNC_DISPATCHER, 
    SYNC_TRANSACTIONS_DISPATCHER, 
    PRICE_TICKER_DISPATCHER,
    TRANSACTION_PROCESSOR_DISPATCHER
)

def sync_platform_balances(platform: InvestmentPlatform):
    """
    Основная логика для синхронизации балансов активов для одной платформы.
    """
    sync_function = SYNC_DISPATCHER.get(platform.name.lower())
    if not sync_function:
        current_app.logger.warning(f"[BG_SYNC] Нет функции синхронизации балансов для платформы '{platform.name}'.")
        return False, f"Нет функции синхронизации для {platform.name}"

    try:
        api_key, api_secret, passphrase = platform.api_key, platform.api_secret, platform.passphrase
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
                    prices_by_ticker[item['ticker']] = Decimal(item['price'])

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

        status_msg = f"Success: {added_count} added, {updated_count} updated, {removed_count} zeroed."
        platform.last_sync_status = status_msg
        platform.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()
        current_app.logger.info(f"[BG_SYNC] Balance sync for '{platform.name}' successful. {status_msg}")
        return True, status_msg

    except Exception as e:
        db.session.rollback()
        status_msg = f"Error: {e}"
        platform.last_sync_status = status_msg
        platform.last_synced_at = datetime.now(timezone.utc)
        db.session.commit()
        current_app.logger.error(f"[BG_SYNC] Balance sync error for '{platform.name}': {e}", exc_info=True)
        return False, status_msg

def sync_platform_transactions(platform: InvestmentPlatform):
    """
    Основная логика для синхронизации транзакций для одной платформы.
    """
    sync_function = SYNC_TRANSACTIONS_DISPATCHER.get(platform.name.lower())
    if not sync_function:
        current_app.logger.warning(f"[BG_SYNC] Нет функции синхронизации транзакций для платформы '{platform.name}'.")
        return False, f"No transaction sync function for {platform.name}"

    try:
        api_key, api_secret, passphrase = platform.api_key, platform.api_secret, platform.passphrase
        end_time_dt = datetime.now(timezone.utc)
        buffer_timedelta = timedelta(days=1)
        last_sync = platform.last_tx_synced_at

        if last_sync and last_sync.tzinfo is None:
            last_sync = last_sync.replace(tzinfo=timezone.utc)
        start_time_dt = (last_sync - buffer_timedelta) if last_sync else (end_time_dt - timedelta(days=2*365))

        fetched_data = sync_function(api_key=api_key, api_secret=api_secret, passphrase=passphrase, start_time_dt=start_time_dt, end_time_dt=end_time_dt, platform=platform)
        existing_tx_ids = {tx.exchange_tx_id for tx in platform.transactions}
        
        processor_class = TRANSACTION_PROCESSOR_DISPATCHER.get(platform.name.lower())
        added_count = 0
        if processor_class:
            processor = processor_class(platform, existing_tx_ids)
            processor.process(fetched_data)
            added_count = processor.added_count

        platform.last_tx_synced_at = end_time_dt
        db.session.commit()
        status_msg = f"Success: {added_count} new transactions found."
        current_app.logger.info(f"[BG_SYNC] Transaction sync for '{platform.name}' successful. {status_msg}")
        return True, status_msg

    except Exception as e:
        db.session.rollback()
        status_msg = f"Error: {e}"
        current_app.logger.error(f"[BG_SYNC] Transaction sync error for '{platform.name}': {e}", exc_info=True)
        return False, status_msg