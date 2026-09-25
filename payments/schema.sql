-- v1: amounts are integer minor units; times are fixed-width UTC ISO-8601.
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id TEXT PRIMARY KEY,
    merchant_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL REFERENCES merchants(merchant_id),
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0 AND amount_minor <= 999999999999),
    currency TEXT NOT NULL CHECK (currency IN ('INR', 'USD', 'EUR')),
    created_at TEXT NOT NULL,
    last_event_at TEXT NOT NULL,
    processed_at TEXT,
    settled_at TEXT,
    initiated_count INTEGER NOT NULL DEFAULT 0 CHECK (initiated_count >= 0),
    processed_count INTEGER NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    settled_count INTEGER NOT NULL DEFAULT 0 CHECK (settled_count >= 0),
    conflict_count INTEGER NOT NULL DEFAULT 0 CHECK (conflict_count >= 0),
    payment_status TEXT NOT NULL DEFAULT 'unknown' CHECK (payment_status IN ('unknown','initiated','processed','failed','conflicted')),
    settlement_status TEXT NOT NULL DEFAULT 'unsettled' CHECK (settlement_status IN ('unsettled','settled')),
    status TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('unknown','initiated','processed','failed','conflicted','settled'))
);
CREATE TABLE IF NOT EXISTS payment_events (
    event_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('payment_initiated','payment_processed','payment_failed','settled')),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestion_conflicts (
    conflict_id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    requested_transaction_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('idempotency_key_reuse','transaction_mismatch')),
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE (event_id, payload_hash, reason)
);
CREATE INDEX IF NOT EXISTS idx_transactions_created ON transactions(created_at, transaction_id);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant_created ON transactions(merchant_id, created_at, transaction_id);
CREATE INDEX IF NOT EXISTS idx_transactions_status_created ON transactions(status, created_at, transaction_id);
CREATE INDEX IF NOT EXISTS idx_transactions_merchant_status_created ON transactions(merchant_id, status, created_at, transaction_id);
CREATE INDEX IF NOT EXISTS idx_events_history ON payment_events(transaction_id, occurred_at, event_id);
CREATE INDEX IF NOT EXISTS idx_conflicts_transaction ON ingestion_conflicts(transaction_id, conflict_id);
CREATE INDEX IF NOT EXISTS idx_unsettled_processed ON transactions(processed_at, transaction_id)
    WHERE processed_count > 0 AND settled_count = 0;
PRAGMA user_version = 1;
