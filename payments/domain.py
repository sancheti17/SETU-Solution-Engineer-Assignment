"""Strict boundary validation and a deterministic, commutative event projection."""
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

EVENT_TYPES = {'payment_initiated', 'payment_processed', 'payment_failed', 'settled'}
STATUSES = {'unknown', 'initiated', 'processed', 'failed', 'conflicted', 'settled'}
CURRENCIES = {'INR', 'USD', 'EUR'}  # Explicitly support only two-decimal currencies.
FIELDS = {'event_id', 'event_type', 'transaction_id', 'merchant_id', 'merchant_name', 'amount', 'currency', 'timestamp'}
ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def timestamp(value, field='timestamp'):
    if not isinstance(value, str) or 'T' not in value:
        raise APIError(422, 'validation_error', f'{field} must be an ISO-8601 datetime with timezone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('missing timezone')
        parsed = parsed.astimezone(timezone.utc)
        if parsed.year < 1970 or parsed.year > 2100:
            raise ValueError('year outside supported range')
        return parsed.isoformat(timespec='microseconds')
    except (ValueError, OverflowError):
        raise APIError(422, 'validation_error', f'{field} must be a timezone-aware datetime in years 1970-2100') from None


def identifier(value, field):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise APIError(422, 'validation_error', f'{field} must be 1-128 ASCII letters, digits, dots, colons, underscores or hyphens; start with a letter/digit')
    return value


def money(minor):
    return f'{minor // 100}.{minor % 100:02d}'


def normalize_event(body):
    if not isinstance(body, dict) or set(body) != FIELDS:
        raise APIError(422, 'validation_error', 'Expected exactly these fields: ' + ', '.join(sorted(FIELDS)))
    for field in ('event_id', 'transaction_id', 'merchant_id'):
        identifier(body[field], field)
    if not isinstance(body['event_type'], str) or body['event_type'] not in EVENT_TYPES:
        raise APIError(422, 'validation_error', 'Unsupported event_type')
    name = body['merchant_name']
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
        raise APIError(422, 'validation_error', 'merchant_name must contain 1-200 printable characters')
    currency = body['currency']
    if not isinstance(currency, str) or currency.upper() not in CURRENCIES:
        raise APIError(422, 'validation_error', 'currency must be INR, USD, or EUR')
    value = body['amount']
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise APIError(422, 'validation_error', 'amount must be a positive decimal with at most two fractional digits')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not Decimal('0') < amount <= Decimal('9999999999.99'):
            raise ValueError('amount outside range')
        scaled = amount * 100
        if scaled != scaled.to_integral_value():
            raise ValueError('fractional minor unit')
        minor = int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        raise APIError(422, 'validation_error', 'amount must be 0.01-9999999999.99 with at most two fractional digits') from None
    event = dict(body, amount=money(minor), merchant_name=name.strip(), currency=currency.upper(), timestamp=timestamp(body['timestamp']))
    canonical = json.dumps(event, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return event, minor, canonical, hashlib.sha256(canonical.encode()).hexdigest()


PROJECTION_UPDATE = '''
UPDATE transactions SET
    initiated_count = initiated_count + (:kind = 'payment_initiated'),
    processed_count = processed_count + (:kind = 'payment_processed'),
    failed_count = failed_count + (:kind = 'payment_failed'),
    settled_count = settled_count + (:kind = 'settled'),
    created_at = MIN(created_at, :occurred),
    last_event_at = MAX(last_event_at, :occurred),
    processed_at = CASE WHEN :kind = 'payment_processed' THEN MIN(COALESCE(processed_at, :occurred), :occurred) ELSE processed_at END,
    settled_at = CASE WHEN :kind = 'settled' THEN MIN(COALESCE(settled_at, :occurred), :occurred) ELSE settled_at END
WHERE transaction_id = :tx
'''
STATUS_UPDATE = '''
UPDATE transactions SET
    payment_status = CASE
        WHEN processed_count > 0 AND failed_count > 0 THEN 'conflicted'
        WHEN processed_count > 0 THEN 'processed'
        WHEN failed_count > 0 THEN 'failed'
        WHEN initiated_count > 0 THEN 'initiated' ELSE 'unknown' END,
    settlement_status = CASE WHEN settled_count > 0 THEN 'settled' ELSE 'unsettled' END,
    status = CASE
        WHEN processed_count > 0 AND failed_count > 0 THEN 'conflicted'
        WHEN failed_count > 0 THEN 'failed'
        WHEN processed_count > 0 AND settled_count > 0 THEN 'settled'
        WHEN processed_count > 0 THEN 'processed'
        WHEN initiated_count > 0 THEN 'initiated' ELSE 'unknown' END
WHERE transaction_id = ?
'''


def record_conflict(db, event, canonical, digest, transaction_id, reason, now):
    result = db.execute('''INSERT INTO ingestion_conflicts
        (event_id, transaction_id, requested_transaction_id, reason, payload_hash, payload_json, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(event_id, payload_hash, reason) DO NOTHING''',
        (event['event_id'], transaction_id, event['transaction_id'], reason, digest, canonical, now))
    if result.rowcount:
        db.execute('UPDATE transactions SET conflict_count = conflict_count + 1 WHERE transaction_id = ?', (transaction_id,))
    return {'error': {'code': reason, 'message': 'Event rejected; conflict retained for investigation', 'transaction_id': transaction_id, 'event_id': event['event_id']}}, 409


def ingest(db, normalized):
    """Caller owns a BEGIN IMMEDIATE transaction. Conflict receipts must commit too."""
    event, minor, canonical, digest = normalized
    now, tx = utcnow(), event['transaction_id']
    existing = db.execute('SELECT payload_hash, transaction_id FROM payment_events WHERE event_id = ?', (event['event_id'],)).fetchone()
    if existing:
        if existing['payload_hash'] == digest:
            return {'event_id': event['event_id'], 'transaction_id': tx, 'duplicate': True}, 200
        return record_conflict(db, event, canonical, digest, existing['transaction_id'], 'idempotency_key_reuse', now)
    existing_tx = db.execute('SELECT merchant_id, amount_minor, currency FROM transactions WHERE transaction_id = ?', (tx,)).fetchone()
    if existing_tx and tuple(existing_tx) != (event['merchant_id'], minor, event['currency']):
        return record_conflict(db, event, canonical, digest, tx, 'transaction_mismatch', now)
    db.execute('INSERT INTO merchants VALUES (?, ?, ?) ON CONFLICT(merchant_id) DO NOTHING', (event['merchant_id'], event['merchant_name'], now))
    if existing_tx is None:
        db.execute('''INSERT INTO transactions(transaction_id, merchant_id, amount_minor, currency, created_at, last_event_at)
            VALUES (?, ?, ?, ?, ?, ?)''', (tx, event['merchant_id'], minor, event['currency'], event['timestamp'], event['timestamp']))
    db.execute('INSERT INTO payment_events VALUES (?, ?, ?, ?, ?, ?, ?)', (event['event_id'], tx, event['event_type'], event['timestamp'], now, digest, canonical))
    db.execute(PROJECTION_UPDATE, {'kind': event['event_type'], 'occurred': event['timestamp'], 'tx': tx})
    db.execute(STATUS_UPDATE, (tx,))
    return {'event_id': event['event_id'], 'transaction_id': tx, 'duplicate': False}, 201
