from flask import Blueprint, request, jsonify, current_app
from fns_client import parse_receipt_qr

api_bp = Blueprint('api', __name__)

@api_bp.route('/parse-qr', methods=['POST'])
def handle_parse_qr():
    """
    API-эндпоинт для парсинга QR-кода чека.
    Принимает JSON с ключом 'qr_string'.
    Возвращает JSON с данными чека или ошибкой.
    """
    data = request.get_json()
    if not data or 'qr_string' not in data:
        return jsonify({'error': 'Необходимо передать qr_string в теле запроса.'}), 400

    qr_string = data['qr_string']
    
    if not current_app.config.get('FNS_API_USERNAME') or not current_app.config.get('FNS_API_PASSWORD'):
        return jsonify({'error': 'Сервис QR-кодов не настроен на сервере.'}), 503

    try:
        parsed_data = parse_receipt_qr(qr_string)
        if parsed_data.get('error'):
            return jsonify(parsed_data), 400
        
        return jsonify(parsed_data), 200

    except Exception as e:
        current_app.logger.error(f"Непредвиденная ошибка при парсинге QR: {e}", exc_info=True)
        return jsonify({'error': f'Внутренняя ошибка сервера: {e}'}), 500