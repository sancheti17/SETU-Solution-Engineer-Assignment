"""All filtering, ordering, pagination, aggregation, and discrepancy selection is SQL."""
import json
from datetime import datetime, timedelta
from .domain import APIError, STATUSES, CURRENCIES, identifier, money, timestamp, utcnow

REASONS = {
    'processed_not_settled': 't.processed_count > 0 AND t.settled_count = 0 AND t.processed_at <= :cutoff',
    'settled_failed_payment': 't.failed_count > 0 AND t.settled_count > 0',
    'settled_without_processed': 't.settled_count > 0 AND t.processed_count = 0',
    'conflicting_payment_outcomes': 't.processed_count > 0 AND t.failed_count > 0',
    'multiple_settlement_events': 't.settled_count > 1',
    'ingestion_conflict': 't.conflict_count > 0',
}
SORTS = {'created_at': 't.created_at', 'last_event_at': 't.last_event_at', 'amount': 't.amount_minor', 'transaction_id': 't.transaction_id'}
BASE_ARGS = {'merchant_id', 'status', 'from', 'to', 'currency'}
PAGE_ARGS = {'page', 'page_size'}
TIME_ARGS = {'as_of', 'grace_hours'}


def check_args(args, allowed):
    if set(args) - allowed:
        raise APIError(422, 'validation_error', 'Unknown query parameters: ' + ', '.join(sorted(set(args) - allowed)))
    for key in args:
        if len(args.getlist(key)) != 1:
            raise APIError(422, 'validation_error', f'Duplicate query parameter: {key}')


def integer(args, key, default, low, high):
    try:
        value = int(args.get(key, str(default)))
    except (ValueError, TypeError):
        raise APIError(422, 'validation_error', f'{key} must be an integer') from None
    if not low <= value <= high:
        raise APIError(422, 'validation_error', f'{key} must be between {low} and {high}')
    return value


def pagination(args):
    page = integer(args, 'page', 1, 1, 1000000)
    size = integer(args, 'page_size', 50, 1, 200)
    return page, size, (page - 1) * size


def filter_sql(args):
    clauses, params = [], {}
    for key, choices in [('merchant_id', None), ('status', STATUSES), ('currency', CURRENCIES)]:
        if key in args:
            value = args[key]
            if choices is not None and value not in choices:
                raise APIError(422, 'validation_error', f'Unsupported {key}')
            if key == 'merchant_id':
                identifier(value, key)
            clauses.append(f't.{key} = :{key}')
            params[key] = value
    if 'from' in args:
        params['start'] = timestamp(args['from'], 'from')
        clauses.append('t.created_at >= :start')
    if 'to' in args:
        params['end'] = timestamp(args['to'], 'to')
        clauses.append('t.created_at < :end')
    if 'start' in params and 'end' in params and params['start'] >= params['end']:
        raise APIError(422, 'validation_error', 'from must be earlier than to')
    return (' AND '.join(clauses) if clauses else '1=1'), params


def reconciliation_clock(args, default_grace):
    as_of = timestamp(args['as_of'], 'as_of') if 'as_of' in args else utcnow()
    grace = integer(args, 'grace_hours', default_grace, 0, 8760)
    cutoff = (datetime.fromisoformat(as_of) - timedelta(hours=grace)).isoformat(timespec='microseconds')
    return {'as_of': as_of, 'grace_hours': grace, 'cutoff': cutoff}


def serialize_transaction(row):
    data = dict(row)
    data['amount'] = money(data['amount_minor'])
    data['merchant'] = {'merchant_id': data['merchant_id'], 'merchant_name': data.pop('merchant_name')}
    data['event_count'] = sum(data[k] for k in ('initiated_count', 'processed_count', 'failed_count', 'settled_count'))
    for reason in REASONS:
        data.pop('flag_' + reason, None)
    return data


def list_transactions(db, args):
    check_args(args, BASE_ARGS | PAGE_ARGS | {'sort_by', 'sort_order'})
    where, params = filter_sql(args)
    page, size, offset = pagination(args)
    sort_key, direction = args.get('sort_by', 'created_at'), args.get('sort_order', 'desc')
    if sort_key not in SORTS or direction not in {'asc', 'desc'}:
        raise APIError(422, 'validation_error', 'Invalid sort_by or sort_order')
    order = SORTS[sort_key] + ' ' + direction
    if sort_key != 'transaction_id':
        order += ', t.transaction_id ' + direction
    total = db.execute('SELECT COUNT(*) FROM transactions t WHERE ' + where, params).fetchone()[0]
    rows = db.execute(f'''SELECT t.*, m.merchant_name FROM transactions t JOIN merchants m USING(merchant_id)
        WHERE {where} ORDER BY {order} LIMIT :limit OFFSET :offset''', dict(params, limit=size, offset=offset)).fetchall()
    return {'data': [serialize_transaction(r) for r in rows], 'pagination': {'page': page, 'page_size': size, 'total': total}}


def transaction_details(db, tx, args):
    identifier(tx, 'transaction_id')
    check_args(args, {'history_page', 'history_page_size', 'conflict_page', 'conflict_page_size'})
    row = db.execute('SELECT t.*, m.merchant_name FROM transactions t JOIN merchants m USING(merchant_id) WHERE transaction_id = ?', (tx,)).fetchone()
    if row is None:
        raise APIError(404, 'not_found', 'Transaction not found')
    result = serialize_transaction(row)
    for prefix, table, order in [('history', 'payment_events', 'occurred_at, event_id'), ('conflict', 'ingestion_conflicts', 'conflict_id')]:
        page = integer(args, prefix + '_page', 1, 1, 1000000)
        size = integer(args, prefix + '_page_size', 100, 1, 200)
        rows = db.execute(f'SELECT * FROM {table} WHERE transaction_id = ? ORDER BY {order} LIMIT ? OFFSET ?', (tx, size, (page - 1) * size)).fetchall()
        records = []
        for r in rows:
            item = dict(r)
            item['payload'] = json.loads(item.pop('payload_json'))
            records.append(item)
        key = 'events' if prefix == 'history' else 'conflicts'
        result[key] = records
        total = db.execute(f'SELECT COUNT(*) FROM {table} WHERE transaction_id = ?', (tx,)).fetchone()[0]
        result[prefix + '_pagination'] = {'page': page, 'page_size': size, 'total': total}
    return result


def discrepancy_sql():
    return '(' + ') OR ('.join(REASONS.values()) + ')'


def discrepancies(db, args, default_grace):
    check_args(args, BASE_ARGS | PAGE_ARGS | TIME_ARGS | {'reason'})
    where, params = filter_sql(args)
    clock = reconciliation_clock(args, default_grace)
    params['cutoff'] = clock['cutoff']
    reason = args.get('reason')
    if reason is not None and reason not in REASONS:
        raise APIError(422, 'validation_error', 'Unsupported reason')
    where += ' AND (' + (REASONS[reason] if reason else discrepancy_sql()) + ')'
    page, size, offset = pagination(args)
    total = db.execute('SELECT COUNT(*) FROM transactions t WHERE ' + where, params).fetchone()[0]
    flags = ', '.join(f'({expr}) AS flag_{name}' for name, expr in REASONS.items())
    rows = db.execute(f'''SELECT t.*, m.merchant_name, {flags} FROM transactions t JOIN merchants m USING(merchant_id)
        WHERE {where} ORDER BY t.created_at DESC, t.transaction_id DESC LIMIT :limit OFFSET :offset''', dict(params, limit=size, offset=offset)).fetchall()
    data = []
    for row in rows:
        item = serialize_transaction(row)
        item['reasons'] = [name for name in REASONS if row['flag_' + name]]
        data.append(item)
    return {'data': data, 'pagination': {'page': page, 'page_size': size, 'total': total}, **clock}


def summary(db, args, default_grace):
    check_args(args, BASE_ARGS | PAGE_ARGS | TIME_ARGS | {'group_by'})
    dimensions = args.get('group_by', 'merchant,date,status').split(',')
    allowed = {'merchant': 't.merchant_id', 'date': 'substr(t.created_at,1,10)', 'status': 't.status'}
    if not dimensions or len(set(dimensions)) != len(dimensions) or any(d not in allowed for d in dimensions):
        raise APIError(422, 'validation_error', 'group_by must be distinct comma-separated values from merchant,date,status')
    where, params = filter_sql(args)
    clock = reconciliation_clock(args, default_grace)
    params['cutoff'] = clock['cutoff']
    page, size, offset = pagination(args)
    groups = [allowed[d] for d in dimensions] + ['t.currency']
    projections = [allowed[d] + ' AS ' + d for d in dimensions] + ['t.currency']
    grouped_sql = f'''SELECT {', '.join(projections)}, COUNT(*) AS transaction_count,
        SUM(t.amount_minor) AS total_amount_minor,
        SUM(CASE WHEN t.processed_count > 0 THEN t.amount_minor ELSE 0 END) AS processed_amount_minor,
        SUM(CASE WHEN t.settled_count > 0 THEN t.amount_minor ELSE 0 END) AS settled_amount_minor,
        SUM(CASE WHEN t.processed_count > 0 AND t.settled_count = 0 THEN t.amount_minor ELSE 0 END) AS outstanding_amount_minor,
        SUM(CASE WHEN {discrepancy_sql()} THEN 1 ELSE 0 END) AS discrepancy_count
        FROM transactions t WHERE {where} GROUP BY {', '.join(groups)}'''
    total = db.execute('SELECT COUNT(*) FROM (' + grouped_sql + ')', params).fetchone()[0]
    rows = db.execute(grouped_sql + f' ORDER BY {", ".join(groups)} LIMIT :limit OFFSET :offset', dict(params, limit=size, offset=offset)).fetchall()
    data = []
    for row in rows:
        item = dict(row)
        for key in ('total_amount', 'processed_amount', 'settled_amount', 'outstanding_amount'):
            item[key] = money(item[key + '_minor'])
        data.append(item)
    return {'data': data, 'group_by': dimensions + ['currency'], 'pagination': {'page': page, 'page_size': size, 'total_groups': total}, **clock}
