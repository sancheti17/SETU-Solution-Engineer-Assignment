import itertools
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from payments import create_app
from payments.db import connect, transaction
from payments.domain import APIError, ingest, normalize_event
from tools.generate_data import generate
from tools.seed import load


def event(kind='payment_initiated', event_id='e1', tx='t1', **changes):
    return dict({'event_id': event_id, 'event_type': kind, 'transaction_id': tx,
        'merchant_id': 'm1', 'merchant_name': 'Test Merchant', 'amount': '100.25',
        'currency': 'INR', 'timestamp': '2026-01-01T00:00:00Z'}, **changes)


class APITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'payments.db')
        self.app = create_app({'TESTING': True, 'DATABASE_PATH': self.path, 'API_KEY': '', 'REQUIRE_API_KEY': False})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def post(self, body, expected=201):
        response = self.client.post('/events', json=body)
        self.assertEqual(response.status_code, expected, response.json)
        return response.json

    def details(self, tx='t1'):
        response = self.client.get('/transactions/' + tx)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def reasons(self, **params):
        response = self.client.get('/reconciliation/discrepancies', query_string={'as_of': '2026-01-03T00:00:00Z', **params})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_healthy_and_empty_queries(self):
        for path in ['/health','/transactions','/reconciliation/summary','/reconciliation/discrepancies']:
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_full_lifecycle_and_history(self):
        for i, kind in enumerate(['payment_initiated','payment_processed','settled']):
            self.post(event(kind, f'e{i}'))
        data = self.details()
        self.assertEqual((data['status'], data['payment_status'], data['settlement_status']), ('settled','processed','settled'))
        self.assertEqual(data['event_count'], 3)
        self.assertEqual(data['amount_minor'], 10025)
        self.assertEqual(data['amount'], '100.25')
        self.assertEqual(self.reasons()['data'], [])

    def test_exact_retries_do_not_add_history(self):
        self.post(event())
        for _ in range(3):
            self.assertTrue(self.post(event(), 200)['duplicate'])
        self.assertEqual(self.details()['event_count'], 1)

    def test_semantically_equivalent_payload_is_duplicate(self):
        self.post(event())
        self.post(event(amount=100.25, currency='inr', timestamp='2026-01-01T05:30:00+05:30'), 200)
        self.assertEqual(self.details()['conflict_count'], 0)

    def test_changed_payload_conflict_is_persisted_once(self):
        self.post(event())
        for _ in range(2):
            self.post(event(amount='200.25'), 409)
        data = self.details()
        self.assertEqual((data['amount'], data['event_count'], data['conflict_count']), ('100.25',1,1))
        self.assertEqual(data['conflicts'][0]['reason'], 'idempotency_key_reuse')
        self.assertIn('ingestion_conflict', self.reasons()['data'][0]['reasons'])

    def test_reused_id_cannot_create_different_transaction(self):
        self.post(event())
        self.post(event(tx='t2'), 409)
        self.assertEqual(self.client.get('/transactions/t2').status_code, 404)
        self.assertEqual(self.details()['conflicts'][0]['requested_transaction_id'], 't2')

    def test_transaction_identity_is_immutable(self):
        self.post(event())
        for i, change in enumerate([{'merchant_id':'m2'}, {'amount':'1.00'}, {'currency':'USD'}]):
            self.post(event('payment_processed', f'bad{i}', **change), 409)
        self.assertEqual(self.details()['status'], 'initiated')
        self.assertEqual(self.details()['conflict_count'], 3)

    def test_out_of_order_all_permutations(self):
        kinds = ['payment_initiated','payment_processed','settled']
        for n, permutation in enumerate(itertools.permutations(range(3))):
            for i in permutation:
                self.post(event(kinds[i], f'e{n}-{i}', tx=f't{n}', timestamp=f'2026-01-01T0{i}:00:00Z'))
            data = self.details(f't{n}')
            self.assertEqual(data['status'], 'settled')
            self.assertEqual(data['created_at'], '2026-01-01T00:00:00.000000+00:00')
            self.assertEqual(data['last_event_at'], '2026-01-01T02:00:00.000000+00:00')

    def test_conflicting_outcomes_do_not_depend_on_arrival(self):
        for n, kinds in enumerate([['payment_processed','payment_failed'], ['payment_failed','payment_processed']]):
            for i, kind in enumerate(kinds):
                self.post(event(kind, f'e{n}-{i}', tx=f't{n}'))
            self.assertEqual(self.details(f't{n}')['status'], 'conflicted')
        self.assertEqual(self.reasons(reason='conflicting_payment_outcomes')['pagination']['total'], 2)

    def test_grace_window_and_exact_boundary(self):
        self.post(event('payment_processed'))
        self.assertEqual(self.reasons(as_of='2026-01-01T23:59:59Z')['data'], [])
        self.assertEqual(self.reasons(as_of='2026-01-02T00:00:00Z')['data'][0]['reasons'], ['processed_not_settled'])

    def test_late_settlement_clears_pending(self):
        self.post(event('payment_processed'))
        self.assertEqual(self.reasons()['pagination']['total'], 1)
        self.post(event('settled','s1'))
        self.assertEqual(self.reasons()['pagination']['total'], 0)

    def test_settled_failed_payment(self):
        self.post(event('payment_failed'))
        self.post(event('settled', 's1'))
        self.assertEqual(self.details()['status'], 'failed')
        self.assertEqual(set(self.reasons()['data'][0]['reasons']), {'settled_failed_payment','settled_without_processed'})

    def test_settlement_only_then_processed(self):
        self.post(event('settled'))
        self.assertEqual(self.details()['status'], 'unknown')
        self.assertIn('settled_without_processed', self.reasons()['data'][0]['reasons'])
        self.post(event('payment_processed','p1'))
        self.assertEqual(self.details()['status'], 'settled')
        self.assertEqual(self.reasons()['data'], [])

    def test_distinct_settlement_ids_are_suspected_duplicates(self):
        self.post(event('payment_processed'))
        self.post(event('settled','s1'))
        self.post(event('settled','s2'))
        self.post(event('settled','s2'), 200)
        self.assertEqual(self.details()['settled_count'], 2)
        self.assertEqual(self.reasons()['data'][0]['reasons'], ['multiple_settlement_events'])

    def test_repeated_processed_events_are_not_conflicts(self):
        self.post(event('payment_processed','p1'))
        self.post(event('payment_processed','p2'))
        self.post(event('settled','s1'))
        self.assertEqual(self.details()['processed_count'], 2)
        self.assertEqual(self.reasons()['data'], [])

    def test_filters_date_range_pagination_and_sort(self):
        for i in range(4):
            self.post(event(event_id=f'e{i}',tx=f't{i}',merchant_id='m2' if i == 3 else 'm1',timestamp=f'2026-01-0{i+1}T00:00:00Z',amount=f'{4-i}.00'))
        response = self.client.get('/transactions', query_string={'merchant_id':'m1','status':'initiated','from':'2026-01-02T00:00:00Z','to':'2026-01-04T00:00:00Z','sort_by':'amount','sort_order':'asc','page_size':1,'page':2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['pagination']['total'], 2)
        self.assertEqual(response.json['data'][0]['transaction_id'], 't1')

    def test_equal_sort_values_have_stable_tie_breaker(self):
        for tx in ['t3','t1','t2']:
            self.post(event(event_id=tx, tx=tx))
        pages = [self.client.get('/transactions', query_string={'page':p,'page_size':1,'sort_order':'asc'}).json['data'][0]['transaction_id'] for p in [1,2,3]]
        self.assertEqual(pages, ['t1','t2','t3'])

    def test_summary_separates_currencies_and_ignores_event_fanout(self):
        for tx, currency, amount in [('t1','INR','100.25'),('t2','USD','9.99')]:
            for i, kind in enumerate(['payment_initiated','payment_processed','settled','settled']):
                self.post(event(kind, f'{tx}-{i}', tx, currency=currency,amount=amount))
        data = self.client.get('/reconciliation/summary?group_by=merchant').json['data']
        self.assertEqual(len(data), 2)
        self.assertEqual([d['transaction_count'] for d in data], [1,1])
        self.assertEqual({d['currency']:d['total_amount_minor'] for d in data}, {'INR':10025,'USD':999})
        self.assertTrue(all(d['discrepancy_count'] == 1 for d in data))

    def test_history_and_conflict_pagination(self):
        for i in range(3):
            self.post(event(event_id=f'e{i}'))
            self.post(event(event_id=f'e{i}',amount='1.00'),409)
        data = self.client.get('/transactions/t1?history_page=2&history_page_size=1&conflict_page=2&conflict_page_size=1').json
        self.assertEqual(data['events'][0]['event_id'], 'e1')
        self.assertEqual(data['history_pagination']['total'], 3)
        self.assertEqual(data['conflict_pagination']['total'], 3)
        self.assertEqual(len(data['conflicts']), 1)

    def test_input_validation(self):
        for change in [{'amount':True},{'amount':'0'},{'amount':'-1'},{'amount':'1.001'},{'amount':'NaN'},{'amount':'Infinity'},{'amount':'1e999999'},{'amount':'10000000000'}, {'currency':'JPY'}, {'event_type':'refund'}, {'event_type':[]}, {'timestamp':'2026-01-01'}, {'timestamp':'2026-01-01T00:00:00'}, {'merchant_id':'bad id'}, {'merchant_name':''}]:
            with self.subTest(change=change):
                self.post(event(**change), 422)
        self.post({**event(), 'extra':1}, 422)
        self.post([], 422)

    def test_money_does_not_round_binary_floats(self):
        self.post(event(amount=0.29))
        self.assertEqual(self.details()['amount_minor'], 29)
        self.post(event(event_id='bad', tx='t2', amount=0.1+0.2), 422)

    def test_invalid_json_content_type_and_oversize(self):
        for raw in ['{','{"event_id":"a","event_id":"b"}','{"amount":NaN}']:
            self.assertEqual(self.client.post('/events',data=raw,content_type='application/json').status_code,400)
        self.assertEqual(self.client.post('/events',data='{}',content_type='text/plain').status_code,415)
        self.assertEqual(self.client.post('/events',data=' ' * 17000,content_type='application/json').status_code,413)

    def test_bad_query_parameters(self):
        for path in ['/transactions?page=0','/transactions?page_size=201','/transactions?sort_by=amount;DROP%20TABLE%20transactions','/transactions?status=whatever','/transactions?status=failed&status=settled','/transactions?from=2026-01-02T00:00:00Z&to=2026-01-01T00:00:00Z','/reconciliation/summary?group_by=merchant,merchant','/reconciliation/summary?group_by=bad','/reconciliation/discrepancies?reason=bad','/reconciliation/discrepancies?grace_hours=-1','/transactions?unknown=x']:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 422)

    def test_auth_and_security_headers(self):
        app = create_app({'TESTING':True,'DATABASE_PATH':self.path,'API_KEY':'x'*24,'REQUIRE_API_KEY':True})
        client = app.test_client()
        self.assertEqual(client.get('/health').status_code,200)
        self.assertEqual(client.get('/transactions').status_code,401)
        response = client.get('/transactions',headers={'X-API-Key':'x'*24})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertTrue(response.headers['X-Request-ID'])
        with self.assertRaises(RuntimeError):
            create_app({'DATABASE_PATH':self.path,'REQUIRE_API_KEY':True,'API_KEY':''})

    def test_json_404_and_405(self):
        self.assertEqual(self.client.get('/transactions/missing').json['error']['code'], 'not_found')
        self.assertEqual(self.client.get('/missing').status_code,404)
        self.assertEqual(self.client.put('/events').status_code,405)

    def test_concurrent_duplicate_ingestion(self):
        def submit(_):
            with self.app.test_client() as client:
                return client.post('/events',json=event()).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(submit, range(24)))
        self.assertEqual(statuses.count(201),1)
        self.assertEqual(statuses.count(200),23)
        self.assertEqual(self.details()['event_count'],1)

    def test_concurrent_distinct_events_same_transaction(self):
        def submit(i):
            with self.app.test_client() as client:
                kind = ['payment_initiated','payment_processed','settled'][i % 3]
                return client.post('/events', json=event(kind, f'parallel-{i}')).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(submit,range(30))),[201]*30)
        self.assertEqual(self.details()['event_count'],30)
        self.assertEqual(self.details()['settled_count'],10)

    def test_busy_database_returns_retryable_503(self):
        self.app.config['DB_TIMEOUT_MS']=10
        db=connect(self.path)
        try:
            db.execute('BEGIN IMMEDIATE')
            response=self.client.post('/events',json=event())
            self.assertEqual(response.status_code,503,response.json)
            self.assertEqual(response.headers['Retry-After'],'1')
        finally:
            db.rollback(); db.close()
        self.post(event())

    def test_transaction_rollback_is_atomic(self):
        db=connect(self.path)
        try:
            with self.assertRaises(RuntimeError):
                with transaction(db,write=True):
                    ingest(db,normalize_event(event()))
                    raise RuntimeError('simulate failure before commit')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM payment_events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM merchants').fetchone()[0],0)
        finally:
            db.close()

    def test_persistence_across_app_restart(self):
        self.post(event())
        other=create_app({'TESTING':True,'DATABASE_PATH':self.path,'API_KEY':'','REQUIRE_API_KEY':False})
        self.assertEqual(other.test_client().get('/transactions/t1').json['event_count'],1)


class DatasetTest(unittest.TestCase):
    def test_full_volume_dataset_and_projection_integrity(self):
        events, manifest=generate()
        self.assertEqual(manifest['total_deliveries'],14870)
        self.assertEqual(len(events),14870)
        with tempfile.TemporaryDirectory() as temp:
            path=str(Path(temp)/'volume.db')
            self.assertEqual(load(events,database=path),{201:13500,200:1350,409:20})
            self.assertEqual(load(events,database=path),{200:14850,409:20})
            db=connect(path)
            try:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM merchants').fetchone()[0],5)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0],5000)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM payment_events').fetchone()[0],13500)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ingestion_conflicts').fetchone()[0],20)
                mismatch=db.execute('''SELECT COUNT(*) FROM transactions t JOIN (
                    SELECT transaction_id, MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at,
                    SUM(event_type='payment_initiated') AS ni, SUM(event_type='payment_processed') AS np,
                    SUM(event_type='payment_failed') AS nf, SUM(event_type='settled') AS ns
                    FROM payment_events GROUP BY transaction_id) e USING(transaction_id)
                    WHERE t.initiated_count!=e.ni OR t.processed_count!=e.np OR t.failed_count!=e.nf OR t.settled_count!=e.ns
                    OR t.created_at!=e.first_at OR t.last_event_at!=e.last_at''').fetchone()[0]
                self.assertEqual(mismatch,0)
                plan=' '.join(str(tuple(r)) for r in db.execute("EXPLAIN QUERY PLAN SELECT * FROM transactions WHERE merchant_id='merchant_1' AND status='settled' ORDER BY created_at,transaction_id LIMIT 50"))
                self.assertIn('idx_transactions_merchant_status_created',plan)
            finally:
                db.close()
            app=create_app({'TESTING':True,'DATABASE_PATH':path,'API_KEY':'','REQUIRE_API_KEY':False})
            client=app.test_client()
            summary=client.get('/reconciliation/summary?group_by=merchant&page_size=200').json
            self.assertEqual(sum(row['transaction_count'] for row in summary['data']),5000)
            discrepancies=client.get('/reconciliation/discrepancies?as_of=2026-02-15T00:00:00Z').json
            self.assertEqual(discrepancies['pagination']['total'],2505)


if __name__ == '__main__':
    unittest.main()
