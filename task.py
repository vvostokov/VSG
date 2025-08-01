from flask import Blueprint, current_app, jsonify
from werkzeug.exceptions import Forbidden

# Импортируем функции для обновления из analytics_logic
from analytics_logic import (
    refresh_securities_portfolio_history,
    refresh_crypto_portfolio_history,
    refresh_securities_price_change_data,
    refresh_crypto_price_change_data,
    refresh_performance_chart_data,
    refresh_market_leaders_cache
)

tasks_bp = Blueprint('tasks', __name__)

@tasks_bp.route('/tasks/refresh-all/<secret_key>')
def trigger_refresh_all(secret_key):
    """
    Защищенный эндпоинт для запуска всех задач по обновлению аналитики.
    Вызывается внешним cron-сервисом.
    """
    # Проверяем секретный ключ из переменных окружения
    if not current_app.config.get('CRON_SECRET_KEY') or secret_key != current_app.config.get('CRON_SECRET_KEY'):
        current_app.logger.warning(f"Failed task trigger attempt with key: {secret_key}")
        raise Forbidden("Invalid or missing secret key.")

    current_app.logger.info("Starting scheduled tasks via secret URL...")
    results = {}
    
    try:
        # Запускаем все задачи обновления по очереди
        results['securities_portfolio_history'] = refresh_securities_portfolio_history()
        results['crypto_portfolio_history'] = refresh_crypto_portfolio_history()
        results['securities_price_change'] = refresh_securities_price_change_data()
        results['crypto_price_change'] = refresh_crypto_price_change_data()
        results['performance_chart'] = refresh_performance_chart_data()
        results['market_leaders'] = refresh_market_leaders_cache()

        current_app.logger.info("Scheduled tasks completed successfully.")
        return jsonify({"status": "success", "details": results})

    except Exception as e:
        current_app.logger.error(f"Error during scheduled task execution: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500