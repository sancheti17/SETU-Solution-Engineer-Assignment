"""Generate deterministic, deliberately out-of-order demo events (no real PII)."""
import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

MERCHANTS = [('merchant_1','Northstar Books'), ('merchant_2','FreshBasket'), ('merchant_3','Metro Electronics'), ('merchant_4','Cloud Kitchen'), ('merchant_5','TravelNest')]
FLOWS = [
    ['payment_initiated','payment_processed','settled'],
    ['payment_initiated','payment_processed','settled'],
    ['payment_initiated','payment_processed','settled'],
    ['payment_initiated','payment_processed','settled'],
    ['payment_initiated','payment_failed'],
    ['payment_initiated','payment_processed'],
    ['payment_initiated','payment_failed','settled'],
    ['payment_initiated','payment_processed','payment_failed'],
    ['payment_initiated','payment_processed','settled','settled'],
    ['settled'],
]


def generate(count=5000, seed=42):
    rng = random.Random(seed)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = []
    for i in range(count):
        merchant_id, merchant_name = MERCHANTS[(i // 10) % len(MERCHANTS)]
        tx = str(uuid5(NAMESPACE_URL, f'setu-demo/{seed}/transaction/{i}'))
        minor = rng.randint(100, 2500000)
        currency = 'EUR' if i % 31 == 0 else ('USD' if i % 17 == 0 else 'INR')
        occurred = start + timedelta(days=i % 28, seconds=(i * 137) % 80000)
        for j, kind in enumerate(FLOWS[i % len(FLOWS)]):
            events.append({'event_id': str(uuid5(NAMESPACE_URL, f'setu-demo/{seed}/event/{i}/{j}')),
                'event_type': kind, 'transaction_id': tx, 'merchant_id': merchant_id, 'merchant_name': merchant_name,
                'amount': f'{minor // 100}.{minor % 100:02d}', 'currency': currency,
                'timestamp': (occurred + timedelta(minutes=j * 5)).isoformat(timespec='microseconds')})
    unique_count = len(events)
    duplicates = [dict(e) for e in events[::10]]
    collisions = [dict(e, merchant_name=e['merchant_name'] + ' [conflicting payload]') for e in events[:20]]
    deliveries = events + duplicates
    rng.shuffle(deliveries)
    # Put reused-key conflicts after accepted originals, so first-writer semantics are deterministic.
    deliveries.extend(collisions)
    manifest = {'seed': seed, 'transactions': count, 'merchants': len({e['merchant_id'] for e in events}),
        'accepted_unique_events': unique_count, 'exact_duplicate_deliveries': len(duplicates),
        'conflicting_deliveries': len(collisions), 'total_deliveries': len(deliveries),
        'reconciliation_as_of': '2026-02-15T00:00:00Z', 'default_grace_hours': 24}
    return deliveries, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='sample_events.json')
    parser.add_argument('--transactions', type=int, default=5000)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    if args.transactions < 50:
        parser.error('--transactions must be at least 50')
    events, manifest = generate(args.transactions, args.seed)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deliberately do not silently replace an existing dataset.
    with path.open('x', encoding='utf-8') as f:
        json.dump(events, f, indent=2)
        f.write('\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
