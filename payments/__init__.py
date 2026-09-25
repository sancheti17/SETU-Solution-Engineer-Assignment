"""Application factory; no database or server work happens on module import."""
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from decimal import Decimal
from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException
from .db import close_db, get_db, initialize, transaction
from .domain import APIError, ingest, normalize_event
from .queries import discrepancies, list_transactions, summary, transaction_details


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE_PATH=os.environ.get('DATABASE_PATH', 'data/payments.db'),
        API_KEY=os.environ.get('API_KEY', ''),
        REQUIRE_API_KEY=os.environ.get('REQUIRE_API_KEY', 'false').lower() == 'true',
        SETTLEMENT_GRACE_HOURS=int(os.environ.get('SETTLEMENT_GRACE_HOURS', '24')),
        DB_TIMEOUT_MS=5000,
        MAX_CONTENT_LENGTH=16384,
        JSON_SORT_KEYS=False,
    )
    if config:
        app.config.update(config)
    if app.config['REQUIRE_API_KEY'] and len(app.config['API_KEY']) < 24:
        raise RuntimeError('Production requires API_KEY with at least 24 characters')
    if not 0 <= app.config['SETTLEMENT_GRACE_HOURS'] <= 8760:
        raise RuntimeError('SETTLEMENT_GRACE_HOURS must be between 0 and 8760')
    initialize(app.config['DATABASE_PATH'])
    app.teardown_appcontext(close_db)

    @app.before_request
    def authorize():
        g.request_id, g.started = str(uuid.uuid4()), time.perf_counter()
        if request.path == '/health':
            return None
        expected = app.config['API_KEY']
        supplied = request.headers.get('X-API-Key', '')
        if expected and not hmac.compare_digest(expected.encode(), supplied.encode()):
            raise APIError(401, 'unauthorized', 'Missing or invalid X-API-Key')
        return None

    @app.after_request
    def headers_and_log(response):
        response.headers['X-Request-ID'] = g.request_id
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        app.logger.info(json.dumps({'request_id': g.request_id, 'method': request.method,
            'route': str(request.url_rule), 'status': response.status_code,
            'duration_ms': round((time.perf_counter() - g.started) * 1000, 2)}))
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error={'code': error.code, 'message': error.message, 'request_id': g.request_id}), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error={'code': error.name.lower().replace(' ', '_'), 'message': error.description, 'request_id': g.request_id}), error.code

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(error):
        if getattr(error, 'sqlite_errorcode', 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            response = jsonify(error={'code': 'database_busy', 'message': 'Retry with backoff and the same event_id', 'request_id': g.request_id})
            response.headers['Retry-After'] = '1'
            return response, 503
        app.logger.exception('Database operation failed')
        return jsonify(error={'code': 'internal_error', 'message': 'Database operation failed', 'request_id': g.request_id}), 500

    @app.errorhandler(Exception)
    def unexpected_error(error):
        app.logger.exception('Unhandled request error')
        return jsonify(error={'code': 'internal_error', 'message': 'Unexpected server error', 'request_id': g.request_id}), 500

    @app.get('/health')
    def health():
        db = get_db()
        db.execute('SELECT transaction_id FROM transactions LIMIT 1').fetchone()
        return jsonify(status='ok', schema_version=db.execute('PRAGMA user_version').fetchone()[0])

    @app.post('/events')
    def events():
        if not request.is_json:
            raise APIError(415, 'unsupported_media_type', 'Use Content-Type: application/json')
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate JSON field')
                result[key] = value
            return result
        def invalid_constant(_value):
            raise ValueError('Non-finite JSON number')
        try:
            body = json.loads(request.get_data(), parse_float=Decimal, parse_constant=invalid_constant, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise APIError(400, 'invalid_json', 'Request body must be valid JSON with unique object keys and finite numbers') from None
        normalized = normalize_event(body)
        with transaction(get_db(), write=True) as db:
            result, status = ingest(db, normalized)
        if status == 409:
            result['error']['request_id'] = g.request_id
        response = jsonify(result)
        if status in (200, 201):
            response.headers['Location'] = '/transactions/' + result['transaction_id']
        return response, status

    @app.get('/transactions')
    def transactions():
        with transaction(get_db()) as db:
            result = list_transactions(db, request.args)
        return jsonify(result)

    @app.get('/transactions/<transaction_id>')
    def detail(transaction_id):
        with transaction(get_db()) as db:
            result = transaction_details(db, transaction_id, request.args)
        return jsonify(result)

    @app.get('/reconciliation/summary')
    def reconciliation_summary():
        with transaction(get_db()) as db:
            result = summary(db, request.args, app.config['SETTLEMENT_GRACE_HOURS'])
        return jsonify(result)

    @app.get('/reconciliation/discrepancies')
    def reconciliation_discrepancies():
        with transaction(get_db()) as db:
            result = discrepancies(db, request.args, app.config['SETTLEMENT_GRACE_HOURS'])
        return jsonify(result)

    return app
