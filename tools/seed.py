"""Replay sample events through the shared ingestion function or the HTTP API."""
import argparse
import json
import os
import sys
import time
from collections import Counter
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from payments.db import connect, initialize, transaction
from payments.domain import ingest, normalize_event


def http_ingest(base_url, event, api_key):
    request = Request(base_url.rstrip('/') + '/events', data=json.dumps(event, default=str).encode(),
        headers={'Content-Type': 'application/json', 'X-API-Key': api_key}, method='POST')
    for attempt in range(5):
        try:
            with urlopen(request, timeout=20) as response:
                return response.status
        except HTTPError as error:
            status = error.code
            error.close()
            if status != 503:
                return status
        except (URLError, TimeoutError):
            if attempt == 4:
                raise
        time.sleep(0.1 * 2 ** attempt)
    return 503


def load(events, database=None, url=None, api_key='', batch_size=500):
    counts = Counter()
    if url:
        for event in events:
            counts[http_ingest(url, event, api_key)] += 1
    else:
        initialize(database)
        db = connect(database)
        try:
            for offset in range(0, len(events), batch_size):
                with transaction(db, write=True):
                    for event in events[offset:offset + batch_size]:
                        _, status = ingest(db, normalize_event(event))
                        counts[status] += 1
        finally:
            db.close()
    return dict(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', default='sample_events.json')
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--database', default=None)
    target.add_argument('--url', help='Replay through POST /events instead of accessing the database')
    parser.add_argument('--expect-conflicts', type=int, default=0)
    args = parser.parse_args()
    with open(args.file, encoding='utf-8') as f:
        events = json.load(f, parse_float=Decimal)
    if not isinstance(events, list):
        parser.error('Input must be a JSON array')
    started = time.perf_counter()
    counts = load(events, database=args.database or os.environ.get('DATABASE_PATH', 'data/payments.db'),
        url=args.url, api_key=os.environ.get('API_KEY', ''))
    print(json.dumps({'deliveries': len(events), 'status_counts': counts, 'seconds': round(time.perf_counter() - started, 3)}, indent=2))
    unexpected = any(status not in (200, 201, 409) for status in counts)
    if unexpected or counts.get(409, 0) != args.expect_conflicts:
        print('Unexpected response counts. Inspect the output; the demo expects exactly 20 conflicts.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
